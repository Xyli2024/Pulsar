"""Live power telemetry from AppleSMC on Apple Silicon Macs."""

import ctypes as ct
from functools import lru_cache
import math
import platform
import plistlib
import struct
import sys

import psutil

from .macos import CF_BINARY_PLIST, CF_UTF8, bind, core_foundation


class _SMCRequest(ct.Structure):
    # AppleSMC's private 80-byte request/response layout.
    _fields_ = [
        ("key", ct.c_uint32),
        ("_version_and_limits", ct.c_uint8 * 24),
        ("data_size", ct.c_uint32),
        ("data_type", ct.c_uint32),
        ("_attributes_and_padding", ct.c_uint8 * 4),
        ("result", ct.c_uint8),
        ("_status", ct.c_uint8),
        ("command", ct.c_uint8),
        ("_data32", ct.c_uint32),
        ("data", ct.c_uint8 * 32),
    ]


_REQUEST_SIZE = ct.sizeof(_SMCRequest)
_READ_BYTES = 5
_READ_KEY_INFO = 9
_SMC_SELECTOR = 2
_DATA_FORMATS = {b"flt ": "<f", b"si16": "<h", b"ui16": "<H", b"si32": "<i", b"ui32": "<I"}


@lru_cache(maxsize=1)
def _load_iokit() -> tuple[ct.CDLL, int] | None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return None

    uint32 = ct.c_uint32
    try:
        iokit = ct.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        libsystem = ct.CDLL("/usr/lib/libSystem.B.dylib")
        task = uint32.in_dll(libsystem, "mach_task_self_").value
        iokit.IOServiceMatching.argtypes = [ct.c_char_p]
        iokit.IOServiceMatching.restype = ct.c_void_p
        iokit.IOServiceGetMatchingService.argtypes = [uint32, ct.c_void_p]
        iokit.IOServiceGetMatchingService.restype = uint32
        iokit.IOServiceOpen.argtypes = [uint32, uint32, uint32, ct.POINTER(uint32)]
        iokit.IOObjectRelease.argtypes = [uint32]
        iokit.IOServiceClose.argtypes = [uint32]
        bind(iokit, "IORegistryEntryCreateCFProperty", ct.c_void_p,
             uint32, ct.c_void_p, ct.c_void_p, uint32)
        iokit.IOConnectCallStructMethod.argtypes = [
            uint32, uint32, ct.POINTER(_SMCRequest), ct.c_size_t,
            ct.POINTER(_SMCRequest), ct.POINTER(ct.c_size_t),
        ]
    except (OSError, AttributeError, ValueError):
        return None
    return iokit, task


def _smc_call(iokit: ct.CDLL, connection: int, request: _SMCRequest) -> _SMCRequest | None:
    response = _SMCRequest()
    size = ct.c_size_t(_REQUEST_SIZE)
    status = iokit.IOConnectCallStructMethod(
        connection, _SMC_SELECTOR, ct.byref(request), _REQUEST_SIZE,
        ct.byref(response), ct.byref(size),
    )
    if status == 0 and response.result == 0 and size.value == _REQUEST_SIZE:
        return response
    return None


def _read_key(iokit: ct.CDLL, connection: int, key: bytes) -> float | None:
    request = _SMCRequest(key=int.from_bytes(key, "big"), command=_READ_KEY_INFO)
    info = _smc_call(iokit, connection, request)
    if info is None:
        return None
    fmt = _DATA_FORMATS.get(info.data_type.to_bytes(4, "big"))
    if fmt is None or info.data_size != struct.calcsize(fmt):
        return None

    request.command = _READ_BYTES
    request.data_size = info.data_size
    response = _smc_call(iokit, connection, request)
    if response is None:
        return None
    value = float(struct.unpack_from(fmt, response.data)[0])
    return value if math.isfinite(value) else None


def read_smc_values(*extra_keys: bytes) -> dict[bytes, float | None]:
    """Read the power sensors and any additional keys in one SMC session."""
    bindings = _load_iokit()
    if bindings is None:
        return {}
    iokit, task = bindings

    service = iokit.IOServiceGetMatchingService(0, iokit.IOServiceMatching(b"AppleSMC"))
    if not service:
        return {}
    connection = ct.c_uint32()
    status = iokit.IOServiceOpen(service, task, 0, ct.byref(connection))
    iokit.IOObjectRelease(service)
    if status != 0:
        return {}

    try:
        return {
            key: _read_key(iokit, connection.value, key)
            for key in (b"PDTR", b"B0AC", b"B0AV", *extra_keys)
        }
    finally:
        iokit.IOServiceClose(connection)


def read_registry_property(class_name: bytes, property_name: bytes):
    """Read a property-list value from the matching IOKit service."""
    bindings = _load_iokit()
    if bindings is None:
        return None
    try:
        cf = core_foundation()
    except (AttributeError, OSError):
        return None
    iokit, _ = bindings
    key = cf.CFStringCreateWithCString(None, property_name, CF_UTF8)
    if not key:
        return None
    try:
        service = iokit.IOServiceGetMatchingService(0, iokit.IOServiceMatching(class_name))
        if not service:
            return None
        try:
            value = iokit.IORegistryEntryCreateCFProperty(service, key, None, 0)
        finally:
            iokit.IOObjectRelease(service)
    finally:
        cf.CFRelease(key)
    if not value:
        return None
    try:
        # Binary property list bridges both dictionaries and scalar values.
        data = cf.CFPropertyListCreateData(None, value, CF_BINARY_PLIST, 0, None)
        if not data:
            return None
        try:
            return plistlib.loads(ct.string_at(cf.CFDataGetBytePtr(data), cf.CFDataGetLength(data)))
        except plistlib.InvalidFileException:
            return None
        finally:
            cf.CFRelease(data)
    finally:
        cf.CFRelease(value)


def read_power(values: dict[bytes, float | None] | None = None, *,
               plugged: bool | None = None) -> tuple[float | None, float | None, float | None]:
    """Return adapter input, signed battery power, and estimated system draw in W."""
    if values is None:
        values = read_smc_values()
        if values:
            try:
                battery = psutil.sensors_battery()
            except (OSError, RuntimeError):
                battery = None
            plugged = battery.power_plugged if battery is not None else None
    if not values:
        return None, None, None
    adapter_w = values.get(b"PDTR")
    current_ma = values.get(b"B0AC")
    voltage_mv = values.get(b"B0AV")

    # The adapter sensor can briefly retain its old value after unplugging.
    if plugged is False:
        adapter_w = 0.0
    elif adapter_w is not None and adapter_w < 0:
        adapter_w = None

    battery_w = None
    if current_ma is not None and voltage_mv is not None and voltage_mv > 0:
        battery_w = voltage_mv * current_ma / 1_000_000

    system_w = None
    if adapter_w is not None and battery_w is not None:
        # Positive battery power goes into the pack. Asynchronous samples can
        # briefly give a negative residual, which is not a valid system draw.
        if adapter_w >= battery_w:
            system_w = adapter_w - battery_w
    return adapter_w, battery_w, system_w
