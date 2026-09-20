#!/usr/bin/env python3
# Landlock wrapper for vmf sandbox runs: restricts THIS process (and the
# exec'd child) to deny every outbound TCP/UDP connect. Bind/listen are
# untouched, so slirp hostfwd inbound (published ports, ssh) keeps
# working while guest-initiated outbound (internet, host services via
# the slirp gateway, the slirp DNS resolver's host sockets) is refused.
# Landlock network support needs kernel ABI >= 4 (Linux 6.7+).
import ctypes
import os
import platform
import sys

if platform.machine() != "x86_64":
    sys.exit("landlock-net-deny: unsupported architecture " + platform.machine())

libc = ctypes.CDLL(None, use_errno=True)

SYS_landlock_create_ruleset = 444
SYS_landlock_add_rule = 445
SYS_landlock_restrict_self = 446
LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_ACCESS_NET_BIND_TCP = 1 << 0
LANDLOCK_ACCESS_NET_CONNECT_TCP = 1 << 1
PR_SET_NO_NEW_PRIVS = 38

libc.syscall.restype = ctypes.c_long


def syscall(nr, a=0, b=0, c=0):
    r = libc.syscall(nr, ctypes.c_long(a), ctypes.c_long(b), ctypes.c_long(c))
    if r < 0:
        e = ctypes.get_errno()
        raise OSError(e, os.strerror(e))
    return r


abi = syscall(SYS_landlock_create_ruleset, 0, 0, LANDLOCK_CREATE_RULESET_VERSION)
if abi < 4:
    sys.exit("landlock-net-deny: kernel Landlock network ABI too old (%d)" % abi)


class RulesetAttr(ctypes.Structure):
    # handled_access_fs, handled_access_net (u64 each)
    _fields_ = [("handled_access_fs", ctypes.c_uint64),
                ("handled_access_net", ctypes.c_uint64)]


attr = RulesetAttr(0, LANDLOCK_ACCESS_NET_CONNECT_TCP)
attr_addr = ctypes.cast(ctypes.byref(attr), ctypes.c_void_p).value
fd = syscall(SYS_landlock_create_ruleset, attr_addr, ctypes.sizeof(attr), 0)

if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
    e = ctypes.get_errno()
    raise OSError(e, os.strerror(e))

syscall(SYS_landlock_restrict_self, fd, 0)

# Everything after this point cannot initiate outbound connections.
os.execvp(sys.argv[1], sys.argv[1:])
