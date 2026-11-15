"""Shared bindings for macOS hardware readers."""

import ctypes as ct
from functools import lru_cache
import plistlib
import subprocess


def bind(library: ct.CDLL, name: str, result, *arguments) -> None:
    function = getattr(library, name)
    function.restype = result
    function.argtypes = list(arguments)


@lru_cache(maxsize=1)
def core_foundation() -> ct.CDLL:
    cf = ct.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    pointer = ct.c_void_p
    bind(cf, "CFRelease", None, pointer)
    bind(cf, "CFStringCreateWithCString", pointer, pointer, ct.c_char_p, ct.c_uint32)
    bind(cf, "CFStringGetCString", ct.c_bool, pointer, pointer, ct.c_long, ct.c_uint32)
    bind(cf, "CFDictionaryGetValue", pointer, pointer, pointer)
    bind(cf, "CFArrayGetCount", ct.c_long, pointer)
    bind(cf, "CFArrayGetValueAtIndex", pointer, pointer, ct.c_long)
    bind(cf, "CFPropertyListCreateData", pointer, pointer, pointer, ct.c_long,
         ct.c_ulong, pointer)
    bind(cf, "CFDataGetLength", ct.c_long, pointer)
    bind(cf, "CFDataGetBytePtr", pointer, pointer)
    return cf


CF_UTF8 = 0x08000100
CF_BINARY_PLIST = 200


def registry_entries(*arguments: str) -> list[dict]:
    result = subprocess.run(
        ["/usr/sbin/ioreg", "-a", *arguments],
        check=True, capture_output=True, timeout=1,
    )
    return plistlib.loads(result.stdout)
