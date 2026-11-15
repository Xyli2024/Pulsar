"""Per-core frequency and component energy from Apple Silicon IOReport."""

import atexit
import ctypes as ct
from functools import lru_cache
import platform
import plistlib
import re
import struct
import subprocess
import time
from contextlib import contextmanager

from .macos import CF_UTF8, bind, core_foundation, registry_entries


_ACTIVE_STATE = re.compile(rb"V([0-9]+)P[0-9]+")
_IDLE_STATES = {b"IDLE", b"DOWN", b"OFF"}
# AppleT6000CLPCv3 aggregate energy reports on macOS 27, measured in nJ.
_CLPC_CPU_ENERGY = 0x1047479059
_CLPC_GPU_ENERGY = 0x194519BF1E
_CLPC_ENERGY = {_CLPC_CPU_ENERGY: "CPU Energy", _CLPC_GPU_ENERGY: "GPU Energy"}


def _core_tables() -> dict[str, tuple[int, tuple[int, ...]]]:
    # The legacy channel names below have been verified on M1 Pro. Other
    # chips use different names and frequency tables, so avoid guessing.
    model = subprocess.check_output(
        ["/usr/sbin/sysctl", "-n", "machdep.cpu.brand_string"], timeout=1,
    ).strip()
    if model != b"Apple M1 Pro":
        return {}

    pmgr = registry_entries("-r", "-n", "pmgr", "-d1")[0]
    frequencies = {}
    for tier, key in (("E", "voltage-states1-sram"), ("P", "voltage-states5-sram")):
        table = tuple(hz for hz, _ in struct.iter_unpack("<II", pmgr[key]))
        if not table or table[0] <= 0 or any(a >= b for a, b in zip(table, table[1:])):
            return {}
        frequencies[tier] = table

    cpu_nodes = registry_entries("-l", "-p", "IODeviceTree", "-r", "-n", "cpus", "-d2")[0]
    cores: dict[str, list[tuple[int, int, int]]] = {"E": [], "P": []}
    for node in cpu_nodes["IORegistryEntryChildren"]:
        if "logical-cpu-id" not in node:
            continue
        tier = node["cluster-type"].rstrip(b"\0").decode("ascii")
        die = int.from_bytes(node["die-id"], "little")
        core = int.from_bytes(node["cluster-core-id"], "little")
        cluster = int.from_bytes(node["die-cluster-id"], "little")
        if die != 0 or core > 9:
            return {}
        cores[tier].append((cluster, core, node["logical-cpu-id"]))

    channels = {}
    for tier, tier_cores in cores.items():
        clusters = sorted({cluster for cluster, _, _ in tier_cores})
        for cluster, core, logical_id in tier_cores:
            name = f"{tier}CPU{clusters.index(cluster)}{core}0"
            if name in channels:
                return {}
            channels[name] = (logical_id, frequencies[tier])
    return channels


def _active_average(previous: tuple[int, ...], current: tuple[int, ...],
                    frequencies: tuple[int, ...]) -> float | None:
    if len(previous) != len(current) or len(current) != len(frequencies):
        return None
    deltas = [new - old for old, new in zip(previous, current)]
    active = sum(deltas)
    if active <= 0 or any(delta < 0 for delta in deltas):
        return None
    return sum(hz * delta for hz, delta in zip(frequencies, deltas)) / active / 1e9


@lru_cache(maxsize=1)
def _load_ioreport() -> ct.CDLL:
    io = ct.CDLL("/usr/lib/libIOReport.dylib")
    pointer, flags = ct.c_void_p, ct.c_uint64
    bind(io, "IOReportCopyChannelsInGroup", pointer, pointer, pointer, flags, flags, flags)
    bind(io, "IOReportCreateSubscription", pointer, pointer, pointer,
         ct.POINTER(pointer), flags, pointer)
    bind(io, "IOReportCreateSamples", pointer, pointer, pointer, pointer)
    bind(io, "IOReportChannelGetChannelName", pointer, pointer)
    bind(io, "IOReportChannelGetChannelID", flags, pointer)
    bind(io, "IOReportSimpleGetIntegerValue", ct.c_int64, pointer, ct.c_int32)
    bind(io, "IOReportStateGetCount", ct.c_int32, pointer)
    bind(io, "IOReportStateGetNameForIndex", pointer, pointer, ct.c_int32)
    bind(io, "IOReportStateGetResidency", ct.c_int64, pointer, ct.c_int32)
    return io


class _IOReportSubscription:
    def __init__(self, offered: int) -> None:
        self.cf = cf = core_foundation()
        self.io = io = _load_ioreport()
        self.channels = ct.c_void_p()
        self.subscription = io.IOReportCreateSubscription(None, offered, ct.byref(self.channels), 0, None)
        self.channels_key = cf.CFStringCreateWithCString(None, b"IOReportChannels", CF_UTF8)
        if not (self.subscription and self.channels and self.channels_key):
            self.close()
            raise RuntimeError("IOReport subscription failed")

    def close(self) -> None:
        for reference in (self.channels_key, self.channels, self.subscription):
            if reference:
                self.cf.CFRelease(reference)

    @contextmanager
    def sample(self):
        sample = self.io.IOReportCreateSamples(self.subscription, self.channels, None)
        try:
            array = self.cf.CFDictionaryGetValue(sample, self.channels_key) if sample else None
            yield array
        finally:
            if sample:
                self.cf.CFRelease(sample)


class _CPUFrequencySampler:
    def __init__(self, core_tables: dict[str, tuple[int, tuple[int, ...]]]) -> None:
        self.tables = core_tables
        self.previous: dict[str, tuple[int, ...]] = {}
        self.cf = cf = core_foundation()
        self.io = io = _load_ioreport()

        group = cf.CFStringCreateWithCString(None, b"CPU Stats", CF_UTF8)
        subgroup = cf.CFStringCreateWithCString(None, b"CPU Core Performance States", CF_UTF8)
        try:
            offered = io.IOReportCopyChannelsInGroup(group, subgroup, 0, 0, 0) if group and subgroup else None
        finally:
            if group:
                cf.CFRelease(group)
            if subgroup:
                cf.CFRelease(subgroup)
        if not offered:
            raise RuntimeError("No CPU performance channels")
        try:
            self.report = _IOReportSubscription(offered)
        finally:
            cf.CFRelease(offered)

    def counters(self) -> dict[str, tuple[int, ...]]:
        io, cf = self.io, self.cf
        counters = {}
        with self.report.sample() as array:
            if not array:
                return counters
            buffer = ct.create_string_buffer(128)
            for index in range(cf.CFArrayGetCount(array)):
                item = cf.CFArrayGetValueAtIndex(array, index)
                name = io.IOReportChannelGetChannelName(item)
                if not name or not cf.CFStringGetCString(name, buffer, len(buffer), CF_UTF8):
                    continue
                channel = buffer.value.decode("utf-8")
                if channel not in self.tables:
                    continue
                _, frequencies = self.tables[channel]
                active_states = {}
                for index in range(io.IOReportStateGetCount(item)):
                    state = io.IOReportStateGetNameForIndex(item, index)
                    if not state or not cf.CFStringGetCString(state, buffer, len(buffer), CF_UTF8):
                        break
                    if buffer.value in _IDLE_STATES:
                        continue
                    match = _ACTIVE_STATE.fullmatch(buffer.value)
                    if not match or int(match[1]) in active_states:
                        break
                    active_states[int(match[1])] = io.IOReportStateGetResidency(item, index)
                else:
                    if set(active_states) == set(range(len(frequencies))) and min(active_states.values()) >= 0:
                        counters[channel] = tuple(active_states[i] for i in range(len(frequencies)))
        return counters

    def read(self, core_count: int) -> list[float | None]:
        current = self.counters()
        readings: list[float | None] = [None] * core_count
        for channel, values in current.items():
            logical_id, frequency_table = self.tables[channel]
            previous = self.previous.get(channel)
            if 0 <= logical_id < core_count and previous is not None:
                readings[logical_id] = _active_average(previous, values, frequency_table)
        self.previous = current
        return readings if any(value is not None for value in readings) else []


@lru_cache(maxsize=1)
def _sampler() -> _CPUFrequencySampler | None:
    if platform.machine() != "arm64":
        return None
    try:
        tables = _core_tables()
        if tables and len({logical_id for logical_id, _ in tables.values()}) == len(tables):
            sampler = _CPUFrequencySampler(tables)
            atexit.register(sampler.report.close)
            return sampler
    except (AttributeError, IndexError, KeyError, OSError, RuntimeError,
            ValueError, plistlib.InvalidFileException, struct.error, subprocess.SubprocessError):
        pass
    return None


def read_cpu_frequencies(core_count: int) -> list[float | None]:
    sampler = _sampler()
    if sampler is None or {logical_id for logical_id, _ in sampler.tables.values()} != set(range(core_count)):
        return []
    return sampler.read(core_count)


def _clpc_subscription(driver_id: int) -> _IOReportSubscription:
    # These reports are not advertised by CopyChannelsInGroup. Supply their
    # driver, IDs, scalar power channel type and nanojoule unit in the legend.
    channel_type = (1 << 32) | (2 << 16) | 1
    unit_nj = (3 << 56) | (118 << 32)
    channels = [
        {
            "DriverID": driver_id,
            "DriverName": "AppleCLPC",
            "IOReportGroupName": "CLPC",
            "IOReportSubGroupName": "Energy Counters",
            "IOReportChannelInfo": {"IOReportChannelUnit": unit_nj},
            "LegendChannel": [channel_id, channel_type, name],
        }
        for channel_id, name in _CLPC_ENERGY.items()
    ]
    payload = plistlib.dumps({"IOReportChannels": channels}, fmt=plistlib.FMT_BINARY)
    cf = core_foundation()
    pointer = ct.c_void_p
    bind(cf, "CFDataCreate", pointer, pointer, ct.c_char_p, ct.c_long)
    bind(cf, "CFPropertyListCreateWithData", pointer, pointer, pointer, ct.c_ulong, pointer, pointer)
    data = cf.CFDataCreate(None, payload, len(payload))
    if not data:
        raise RuntimeError("Unable to allocate CLPC legend")
    try:
        # IOReport caches metadata by mutating the channel dictionaries.
        legend = cf.CFPropertyListCreateWithData(None, data, 1, None, None)
    finally:
        cf.CFRelease(data)
    if not legend:
        raise RuntimeError("Invalid CLPC legend")
    try:
        return _IOReportSubscription(legend)
    finally:
        cf.CFRelease(legend)


class _ComponentPowerSampler:
    def __init__(self, report: _IOReportSubscription) -> None:
        self.report = report
        self.previous: dict[int, int] = {}
        self.previous_time = time.monotonic()

    def read(self) -> tuple[float | None, float | None]:
        io, cf = self.report.io, self.report.cf
        current = {}
        now = time.monotonic()
        with self.report.sample() as array:
            if array:
                for index in range(cf.CFArrayGetCount(array)):
                    item = cf.CFArrayGetValueAtIndex(array, index)
                    channel_id = io.IOReportChannelGetChannelID(item)
                    energy_nj = io.IOReportSimpleGetIntegerValue(item, 0)
                    if channel_id in _CLPC_ENERGY and energy_nj > 0:
                        current[channel_id] = energy_nj

        elapsed = now - self.previous_time
        watts = {}
        for channel_id, energy_nj in current.items():
            previous = self.previous.get(channel_id)
            if previous is None or energy_nj < previous or elapsed <= 0:
                continue
            # The GPU can turn off entirely. A flat CPU counter while this
            # process is sampling is stale rather than a zero-watt reading.
            if channel_id == _CLPC_CPU_ENERGY and energy_nj == previous:
                continue
            watts[channel_id] = (energy_nj - previous) / 1e9 / elapsed
        self.previous, self.previous_time = current, now
        return watts.get(_CLPC_CPU_ENERGY), watts.get(_CLPC_GPU_ENERGY)


@lru_cache(maxsize=1)
def _component_power_sampler() -> _ComponentPowerSampler | None:
    if platform.machine() != "arm64" or platform.mac_ver()[0].split(".")[0] != "27":
        return None
    try:
        entries = registry_entries("-r", "-c", "AppleCLPC", "-d1")
        driver = next((entry for entry in entries if entry.get("CFBundleIdentifier")
                       == "com.apple.driver.AppleT6000CLPCv3"), None)
        if driver is not None:
            report = _clpc_subscription(driver["IORegistryEntryID"])
            atexit.register(report.close)
            return _ComponentPowerSampler(report)
    except (AttributeError, KeyError, OSError, RuntimeError, ValueError,
            plistlib.InvalidFileException, subprocess.SubprocessError):
        pass
    return None


def read_component_power() -> tuple[float | None, float | None]:
    """Read interval-average CPU and GPU power in W when available."""
    sampler = _component_power_sampler()
    return sampler.read() if sampler is not None else (None, None)
