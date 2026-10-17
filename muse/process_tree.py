"""Own command descendants on POSIX process groups and Windows Job Objects."""
import os
import platform
import signal


class ProcessTree:
    def __init__(self, process):
        self.process = process
        self.job = None
        if platform.system() == 'Windows':
            self._attach_windows_job()

    def _attach_windows_job(self):
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [('ProcessTime', c.c_int64), ('JobTime', c.c_int64), ('Flags', w.DWORD),
                        ('MinWorkingSet', c.c_size_t), ('MaxWorkingSet', c.c_size_t),
                        ('ActiveProcesses', w.DWORD), ('Affinity', c.c_size_t),
                        ('Priority', w.DWORD), ('Scheduling', w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(name, c.c_uint64) for name in ('ReadOps', 'WriteOps', 'OtherOps', 'ReadBytes', 'WriteBytes', 'OtherBytes')]
        class Extended(c.Structure):
            _fields_ = [('Basic', Limits), ('IO', IO), ('ProcessMemory', c.c_size_t),
                        ('JobMemory', c.c_size_t), ('PeakProcessMemory', c.c_size_t), ('PeakJobMemory', c.c_size_t)]

        api = c.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        api.CreateJobObjectW.restype = w.HANDLE
        api.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        api.SetInformationJobObject.restype = w.BOOL
        api.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        api.OpenProcess.restype = w.HANDLE
        api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        api.AssignProcessToJobObject.restype = w.BOOL
        api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        api.TerminateJobObject.restype = w.BOOL
        api.CloseHandle.argtypes = [w.HANDLE]
        api.CloseHandle.restype = w.BOOL
        job = api.CreateJobObjectW(None, None)
        if not job:
            raise c.WinError(c.get_last_error())
        try:
            limits = Extended()
            limits.Basic.Flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits)):
                raise c.WinError(c.get_last_error())
            handle = api.OpenProcess(0x0100 | 0x0001, False, self.process.pid)  # SET_QUOTA | TERMINATE
            if not handle:
                raise c.WinError(c.get_last_error())
            try:
                if not api.AssignProcessToJobObject(job, handle):
                    raise c.WinError(c.get_last_error())
            finally:
                api.CloseHandle(handle)
        except BaseException:
            api.CloseHandle(job)
            raise
        self.api, self.job = api, job

    def kill(self):
        if self.job is not None:
            # Closing the owned job also stops descendants after the shell exits.
            self.api.CloseHandle(self.job)
            self.job = None
        elif platform.system() != 'Windows':
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
