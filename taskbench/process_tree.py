"""Run a process and kill its whole tree.

POSIX starts the child in its own process group (``setpgid(0, 0)``) and
kills that group with ``killpg`` (the same signal ``kill -- -PGID`` sends).
The group stays in taskbench's session so an approved companion can join it.
Linux also sets the parent-death signal so a hard crash of taskbench delivers
SIGKILL to that child. Grandchildren stay in the group and are killed with
it. On watcher startup, a saved process-group id is killed again so a crash
cannot leave them behind.

Windows creates a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``,
assigns the suspended process to it, then resumes the main thread. Closing
the job or calling ``TerminateJobObject`` kills the tree. If job assignment
is refused, the fallback is ``taskkill /T /F``, which the spec also allows.

``spawn_companion`` puts a server or other extra process in the same group
or job so completion, cancel, timeout, and shutdown kill it too.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

KILL_METHOD = "killpg" if os.name != "nt" else "job_object"


class ProcessTreeError(RuntimeError):
    pass


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def listening_ports(pids: set[int]) -> list[int]:
    if not pids:
        return []
    if sys.platform.startswith("linux"):
        return _linux_listening_ports(pids)
    if os.name == "nt":
        return _windows_listening_ports(pids)
    return []


def kill_process_group(pgid: int) -> None:
    """Kill a saved POSIX process group. Used when recovering after a crash."""
    if os.name == "nt":
        return
    if pgid <= 0 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError:
        return


class ManagedProcess:
    """A child plus the group or job that contains its descendants."""

    def __init__(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        if not argv:
            raise ProcessTreeError("empty argv")
        self.argv = list(argv)
        self.cwd = cwd
        self.queue: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self.stdout_eof = False
        self.stderr_eof = False
        self._killed = False
        self.companions: list[subprocess.Popen[bytes]] = []
        self._threads: list[threading.Thread] = []
        if os.name == "nt":
            self._start_windows(argv, cwd, env)
        else:
            self._start_posix(argv, cwd, env)

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def group_id(self) -> int:
        return self._group_id

    def send_line(self, text: str) -> None:
        if self._stdin is None:
            return
        try:
            self._stdin.write((text + "\n").encode("utf-8"))
            self._stdin.flush()
        except (BrokenPipeError, OSError):
            return

    def poll_line(self) -> tuple[str, str] | None:
        while True:
            try:
                name, line = self.queue.get_nowait()
            except queue.Empty:
                return None
            if line is None:
                if name == "stdout":
                    self.stdout_eof = True
                else:
                    self.stderr_eof = True
                continue
            return name, line

    def output_closed(self) -> bool:
        return self.stdout_eof and self.stderr_eof

    def exit_code(self) -> int | None:
        if os.name == "nt":
            return _win_exit_code(self._process_handle)
        assert self._proc is not None
        return self._proc.poll()

    def snapshot_pids(self) -> set[int]:
        found = {self._pid}
        if os.name == "nt":
            found.update(_win_job_pids(self._job))
        else:
            found.update(_posix_group_pids(self._group_id))
        for companion in self.companions:
            if companion.pid:
                found.add(companion.pid)
        return {pid for pid in found if pid > 0}

    def spawn_companion(self, argv: list[str], cwd: Path) -> int:
        if os.name == "nt":
            return _win_spawn_companion(self, argv, cwd)
        group = self._group_id

        def _join_group() -> None:
            os.setpgid(0, group)

        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            preexec_fn=_join_group,
        )
        try:
            os.setpgid(proc.pid, group)
        except OSError:
            pass
        self.companions.append(proc)
        return proc.pid

    def reap_group(self) -> None:
        """Kill the group after the leader has exited.

        Descendants inherit the leader's pipes. Leaving them alive stalls
        completion, because stdout never reaches EOF. This does not mark the
        process finished; ``kill_tree`` still runs at the terminal transition.
        """
        if os.name == "nt":
            self.kill_tree()
            return
        if self._group_id > 0 and self._group_id != os.getpgrp():
            try:
                os.killpg(self._group_id, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def kill_tree(self) -> None:
        if self._killed:
            return
        self._killed = True
        if os.name == "nt":
            _win_kill(self)
        else:
            if self._group_id != os.getpgrp():
                try:
                    os.killpg(self._group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            assert self._proc is not None
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            for companion in self.companions:
                try:
                    companion.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        self._close_stdin()

    def close(self) -> None:
        self.kill_tree()
        if os.name == "nt":
            _win_close_handles(self)

    def _close_stdin(self) -> None:
        if self._stdin is None:
            return
        try:
            self._stdin.close()
        except OSError:
            pass
        self._stdin = None

    def _start_posix(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        def _preexec() -> None:
            os.setpgid(0, 0)
            if sys.platform.startswith("linux"):
                try:
                    import ctypes

                    libc = ctypes.CDLL(None, use_errno=True)
                    # PR_SET_PDEATHSIG = 1, SIGKILL = 9
                    libc.prctl(1, 9)
                    if os.getppid() == 1:
                        os._exit(1)
                except Exception:
                    pass

        self._proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=_preexec,
        )
        self._pid = self._proc.pid
        self._group_id = self._proc.pid
        try:
            os.setpgid(self._pid, self._pid)
        except OSError:
            pass
        self._stdin = self._proc.stdin
        self._process_handle = None
        self._job = None
        self._thread_handle = None
        self.kill_method = "killpg"
        self._start_reader(self._proc.stdout, "stdout")
        self._start_reader(self._proc.stderr, "stderr")

    def _start_reader(self, stream, name: str) -> None:
        def _run() -> None:
            try:
                while True:
                    chunk = stream.readline()
                    if not chunk:
                        break
                    if isinstance(chunk, bytes):
                        text = chunk.decode("utf-8", errors="replace")
                    else:
                        text = chunk
                    self.queue.put((name, text))
            finally:
                self.queue.put((name, None))
                try:
                    stream.close()
                except OSError:
                    pass

        thread = threading.Thread(target=_run, name=f"taskbench-{name}", daemon=True)
        thread.start()
        self._threads.append(thread)

    def _start_windows(self, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
        _win_start(self, argv, cwd, env)


def _posix_group_pids(pgid: int) -> set[int]:
    found: set[int] = set()
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            end = stat.rfind(")")
            fields = stat[end + 2 :].split()
            if int(fields[2]) == pgid:
                found.add(int(entry.name))
        except (OSError, IndexError, ValueError):
            continue
    return found


def _linux_listening_ports(pids: set[int]) -> list[int]:
    inodes: set[str] = set()
    for pid in pids:
        fd_dir = Path("/proc") / str(pid) / "fd"
        try:
            if not fd_dir.is_dir():
                continue
            entries = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in entries:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                inodes.add(target[8:-1])
    ports: set[int] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.is_file():
            continue
        lines = table.read_text(encoding="utf-8", errors="replace").splitlines()[1:]
        for line in lines:
            fields = line.split()
            if len(fields) < 10:
                continue
            state = fields[3]
            if state.upper() != "0A":  # TCP_LISTEN
                continue
            inode = fields[9]
            if inode not in inodes:
                continue
            local = fields[1]
            port_hex = local.split(":")[-1]
            try:
                ports.add(int(port_hex, 16))
            except ValueError:
                continue
    return sorted(ports)


def _windows_listening_ports(pids: set[int]) -> list[int]:
    try:
        output = subprocess.check_output(
            ["netstat", "-ano", "-p", "tcp"],
            text=True,
            errors="replace",
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    ports: set[int] = set()
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0].upper() != "TCP":
            continue
        if parts[3].upper() != "LISTENING":
            continue
        try:
            pid = int(parts[4])
        except ValueError:
            continue
        if pid not in pids:
            continue
        local = parts[1]
        port_text = local.rsplit(":", 1)[-1]
        try:
            ports.add(int(port_text))
        except ValueError:
            continue
    return sorted(ports)


# Windows implementation ----------------------------------------------------

def _win_pid_alive(pid: int) -> bool:
    kernel32, wintypes = _win_kernel()
    access = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    handle = kernel32.OpenProcess(access, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    ok = kernel32.GetExitCodeProcess(handle, ctypes_byref(code))
    kernel32.CloseHandle(handle)
    if not ok:
        return False
    return code.value == 259  # STILL_ACTIVE


def _win_exit_code(handle) -> int | None:
    if not handle:
        return None
    kernel32, wintypes = _win_kernel()
    code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(handle, ctypes_byref(code)):
        return None
    if code.value == 259:
        return None
    return int(code.value)


def _win_kill(proc: ManagedProcess) -> None:
    kernel32, _wintypes = _win_kernel()
    if proc._job:
        kernel32.TerminateJobObject(proc._job, 1)
        kernel32.CloseHandle(proc._job)
        proc._job = None
        proc.kill_method = "job_object"
    elif proc.kill_method == "taskkill":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    if proc._process_handle:
        kernel32.WaitForSingleObject(proc._process_handle, 2000)


def _win_close_handles(proc: ManagedProcess) -> None:
    kernel32, _wintypes = _win_kernel()
    for handle in (proc._process_handle, proc._thread_handle, proc._job):
        if handle:
            kernel32.CloseHandle(handle)
    proc._process_handle = None
    proc._thread_handle = None
    proc._job = None


def _win_job_pids(job) -> set[int]:
    if not job:
        return set()
    import ctypes
    from ctypes import wintypes

    kernel32, _ = _win_kernel()

    class JOBOBJECT_BASIC_PROCESS_ID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * 256),
        ]

    info = JOBOBJECT_BASIC_PROCESS_ID_LIST()
    # JobObjectBasicProcessIdList = 3
    if not kernel32.QueryInformationJobObject(
        job, 3, ctypes.byref(info), ctypes.sizeof(info), None
    ):
        return set()
    count = min(int(info.NumberOfProcessIdsInList), 256)
    return {int(info.ProcessIdList[index]) for index in range(count)}


def _win_start(proc: ManagedProcess, argv: list[str], cwd: Path, env: dict[str, str]) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32, _ = _win_kernel()
    sa_type, startup_type, process_type, job_info_type = _win_structs()

    sa = sa_type()
    sa.nLength = ctypes.sizeof(sa)
    sa.bInheritHandle = True
    sa.lpSecurityDescriptor = None

    def make_pipe():
        read = wintypes.HANDLE()
        write = wintypes.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(read), ctypes.byref(write), ctypes.byref(sa), 0):
            raise ProcessTreeError(f"CreatePipe failed: {ctypes.get_last_error()}")
        return read, write

    stdin_read, stdin_write = make_pipe()
    stdout_read, stdout_write = make_pipe()
    stderr_read, stderr_write = make_pipe()
    inherit = 0x00000001
    # Parent ends must not be inherited.
    kernel32.SetHandleInformation(stdin_write, inherit, 0)
    kernel32.SetHandleInformation(stdout_read, inherit, 0)
    kernel32.SetHandleInformation(stderr_read, inherit, 0)

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise ProcessTreeError(f"CreateJobObject failed: {ctypes.get_last_error()}")
    info = job_info_type()
    info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        raise ProcessTreeError(f"SetInformationJobObject failed: {ctypes.get_last_error()}")

    startup = startup_type()
    startup.cb = ctypes.sizeof(startup)
    startup.dwFlags = 0x00000100  # STARTF_USESTDHANDLES
    startup.hStdInput = stdin_read
    startup.hStdOutput = stdout_write
    startup.hStdError = stderr_write
    process_info = process_type()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    env_block = ctypes.create_unicode_buffer(_env_block(env))
    application = argv[0] if os.path.dirname(argv[0]) else None
    flags = 0x00000004 | 0x00000400 | 0x08000000  # SUSPENDED | UNICODE_ENV | NO_WINDOW
    created = kernel32.CreateProcessW(
        application,
        cmdline,
        None,
        None,
        True,
        flags,
        ctypes.cast(env_block, ctypes.c_void_p),
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    )
    if not created:
        error = ctypes.get_last_error()
        kernel32.CloseHandle(job)
        raise ProcessTreeError(f"CreateProcess failed: {error}")

    assigned = kernel32.AssignProcessToJobObject(job, process_info.hProcess)
    kernel32.ResumeThread(process_info.hThread)
    proc._pid = int(process_info.dwProcessId)
    proc._group_id = proc._pid
    proc._process_handle = process_info.hProcess
    proc._thread_handle = process_info.hThread
    proc._proc = None
    proc.kill_method = "job_object" if assigned else "taskkill"
    if assigned:
        proc._job = job
    else:
        kernel32.CloseHandle(job)
        proc._job = None

    kernel32.CloseHandle(stdin_read)
    kernel32.CloseHandle(stdout_write)
    kernel32.CloseHandle(stderr_write)

    def as_file(handle, mode: str, flags: int):
        descriptor = msvcrt.open_osfhandle(int(handle.value), flags)
        return os.fdopen(descriptor, mode, buffering=0)

    binary = getattr(os, "O_BINARY", 0)
    proc._stdin = as_file(stdin_write, "wb", os.O_WRONLY | binary)
    stdout = as_file(stdout_read, "rb", os.O_RDONLY | binary)
    stderr = as_file(stderr_read, "rb", os.O_RDONLY | binary)
    proc._start_reader(stdout, "stdout")
    proc._start_reader(stderr, "stderr")


def _win_spawn_companion(owner: ManagedProcess, argv: list[str], cwd: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32, _ = _win_kernel()
    _sa_type, startup_type, process_type, _job_info = _win_structs()
    startup = startup_type()
    startup.cb = ctypes.sizeof(startup)
    process_info = process_type()
    cmdline = ctypes.create_unicode_buffer(subprocess.list2cmdline(argv))
    flags = 0x00000004 | 0x08000000  # SUSPENDED | NO_WINDOW
    application = argv[0] if os.path.dirname(argv[0]) else None
    if not kernel32.CreateProcessW(
        application,
        cmdline,
        None,
        None,
        False,
        flags,
        None,
        str(cwd),
        ctypes.byref(startup),
        ctypes.byref(process_info),
    ):
        raise ProcessTreeError(f"companion CreateProcess failed: {ctypes.get_last_error()}")
    if owner._job:
        kernel32.AssignProcessToJobObject(owner._job, process_info.hProcess)
    kernel32.ResumeThread(process_info.hThread)
    pid = int(process_info.dwProcessId)
    kernel32.CloseHandle(process_info.hThread)
    kernel32.CloseHandle(process_info.hProcess)
    return pid


def _env_block(env: dict[str, str]) -> str:
    parts = [f"{key}={value}\0" for key, value in sorted(env.items())]
    return "".join(parts) + "\0"


def ctypes_byref(value):
    import ctypes

    return ctypes.byref(value)


_WIN_CACHE: dict[str, object] = {}


def _win_kernel():
    import ctypes
    from ctypes import wintypes

    cached = _WIN_CACHE.get("kernel")
    if cached:
        return cached
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL
    kernel32.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    kernel32.SetHandleInformation.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    _WIN_CACHE["kernel"] = (kernel32, wintypes)
    return kernel32, wintypes


def _win_structs():
    import ctypes
    from ctypes import wintypes

    cached = _WIN_CACHE.get("structs")
    if cached:
        return cached

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadOperationCount2", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    # The field above accidentally duplicated. Rebuild IO_COUNTERS correctly.
    class IO_COUNTERS(ctypes.Structure):  # noqa: F811
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    structs = (
        SECURITY_ATTRIBUTES,
        STARTUPINFOW,
        PROCESS_INFORMATION,
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    )
    _WIN_CACHE["structs"] = structs
    return structs
