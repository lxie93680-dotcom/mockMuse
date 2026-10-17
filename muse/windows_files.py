"""Windows handle operations used ONLY by the host publication protocol.

No replace-existing rename is exposed: an editor creating the destination wins.
Imports stay lazy so the transaction state machine can be tested on other hosts.
"""
import ctypes as c
import hashlib
import os
import re
from contextlib import contextmanager
from pathlib import Path


def valid_relative(value):
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Windows 文件路径无效')
    for part in value.split('/'):
        if (part in ('', '.', '..') or part[-1:] in (' ', '.') or
                any(ch in part for ch in '<>:"|?*') or any(ord(ch) < 32 for ch in part) or
                re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', part)):
            raise ValueError('Windows 路径包含保留名称、数据流或非普通路径')
    return value


def reparse(path):
    return bool(path.lstat().st_file_attributes & 0x400) if hasattr(path.lstat(), 'st_file_attributes') else path.is_symlink()


class WindowsFiles:
    def __init__(self):
        from ctypes import wintypes as w
        self.w = w
        self.api = a = c.WinDLL('kernel32', use_last_error=True)
        self.security = s = c.WinDLL('advapi32', use_last_error=True)
        signatures = {
            'CreateFileW': ([w.LPCWSTR,w.DWORD,w.DWORD,c.c_void_p,w.DWORD,w.DWORD,w.HANDLE],w.HANDLE),
            'CloseHandle': ([w.HANDLE],w.BOOL),
            'SetFileInformationByHandle': ([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'ReadFile': ([w.HANDLE,c.c_void_p,w.DWORD,c.POINTER(w.DWORD),c.c_void_p],w.BOOL),
            'SetFilePointerEx': ([w.HANDLE,c.c_int64,c.c_void_p,w.DWORD],w.BOOL),
            'GetFileInformationByHandle': ([w.HANDLE,c.c_void_p],w.BOOL),
            'GetFileInformationByHandleEx': ([w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            'SetFileAttributesW': ([w.LPCWSTR,w.DWORD],w.BOOL),
            'CopyFileExW': ([w.LPCWSTR,w.LPCWSTR,c.c_void_p,c.c_void_p,c.c_void_p,w.DWORD],w.BOOL),
            'GetVolumePathNameW': ([w.LPCWSTR,w.LPWSTR,w.DWORD],w.BOOL),
            'GetVolumeInformationW': ([w.LPCWSTR,w.LPWSTR,w.DWORD,c.c_void_p,c.c_void_p,c.c_void_p,w.LPWSTR,w.DWORD],w.BOOL),
            'GetDriveTypeW': ([w.LPCWSTR],w.UINT),
        }
        for name,(args,result) in signatures.items():
            fn=getattr(a,name);fn.argtypes=args;fn.restype=result
        s.GetKernelObjectSecurity.argtypes=[w.HANDLE,w.DWORD,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]
        s.GetKernelObjectSecurity.restype=w.BOOL
        s.SetFileSecurityW.argtypes=[w.LPCWSTR,w.DWORD,c.c_void_p];s.SetFileSecurityW.restype=w.BOOL
        s.GetSecurityDescriptorControl.argtypes=[c.c_void_p,c.POINTER(w.WORD),c.POINTER(w.DWORD)]
        s.GetSecurityDescriptorControl.restype=w.BOOL

    @staticmethod
    def path(path):
        value=str(Path(path).absolute())
        return value if value.startswith('\\\\?\\') else '\\\\?\\'+value

    @staticmethod
    def check(result):
        if not result: raise c.WinError(c.get_last_error())
        return result

    def check_volume(self,path):
        if str(path).startswith('\\\\'):raise RuntimeError('Windows 文件事务暂只支持本机固定 NTFS 卷')
        root=c.create_unicode_buffer(32768);fs=c.create_unicode_buffer(64)
        self.check(self.api.GetVolumePathNameW(str(path),root,len(root)))
        self.check(self.api.GetVolumeInformationW(root.value,None,0,None,None,None,fs,len(fs)))
        if fs.value.upper()!='NTFS' or self.api.GetDriveTypeW(root.value)!=3:
            raise RuntimeError('Windows 文件事务暂只支持本机固定 NTFS 卷，不支持网络盘或其他文件系统')

    @contextmanager
    def guard(self,path,directory=False,read_only=False):
        # Directories allow reading/creating children, but deny DELETE sharing:
        # their names cannot be replaced while child paths are being published.
        access=0x80000000 | 0x00020000 | (0 if read_only else 0x00010000)
        share=7 if read_only else (3 if directory else 0)
        handle=self.api.CreateFileW(self.path(path),access,share,None,3,0x02200000,None)
        if handle==c.c_void_p(-1).value:raise c.WinError(c.get_last_error())
        try:
            signature,identity=self.inspect(handle,directory)
            yield WindowsHandle(self,handle,signature,identity)
        finally:self.api.CloseHandle(handle)

    def security_bytes(self,handle):
        size=self.w.DWORD()
        self.security.GetKernelObjectSecurity(handle,7,None,0,c.byref(size))
        if not size.value:raise c.WinError(c.get_last_error())
        data=c.create_string_buffer(size.value)
        self.check(self.security.GetKernelObjectSecurity(handle,7,data,len(data),c.byref(size)))
        return data.raw[:size.value]

    def inspect(self,handle,directory):
        w=self.w
        class Info(c.Structure):
            _fields_=[('attrs',w.DWORD),('created',w.FILETIME),('accessed',w.FILETIME),('written',w.FILETIME),
                      ('volume',w.DWORD),('size_high',w.DWORD),('size_low',w.DWORD),('links',w.DWORD),
                      ('index_high',w.DWORD),('index_low',w.DWORD)]
        info=Info();self.check(self.api.GetFileInformationByHandle(handle,c.byref(info)))
        if info.attrs & 0x400 or not directory and info.links!=1:
            raise ValueError('Windows 文件事务拒绝重解析点或硬链接')
        # Named data streams are not editable by the controlled tools. Refuse
        # rather than publish a version whose side streams were never checked.
        buffer=c.create_string_buffer(65536)
        c.set_last_error(0)
        streams=self.api.GetFileInformationByHandleEx(handle,7,buffer,len(buffer))
        stream_error=c.get_last_error()
        if stream_error==38:pass  # Directories may have no data streams, even on success.
        elif streams:
            offset=0
            while True:
                next_offset=c.c_uint32.from_buffer(buffer,offset).value
                name_length=c.c_uint32.from_buffer(buffer,offset+4).value
                name=buffer.raw[offset+24:offset+24+name_length].decode('utf-16-le')
                if name not in ('','::$DATA'):raise ValueError('Windows 文件事务暂不支持 NTFS 命名数据流')
                if not next_offset:break
                offset+=next_offset
        else:raise c.WinError(stream_error)
        result={'kind':'dir' if directory else 'file','attrs':info.attrs & (1|2|4),
                'security':hashlib.sha256(self.security_bytes(handle)).hexdigest()}
        if not directory:
            self.check(self.api.SetFilePointerEx(handle,0,None,0))
            digest=hashlib.sha256();buffer=c.create_string_buffer(1024*1024);count=w.DWORD()
            while True:
                self.check(self.api.ReadFile(handle,buffer,len(buffer),c.byref(count),None))
                if not count.value:break
                digest.update(buffer.raw[:count.value])
            result.update(size=(info.size_high<<32)|info.size_low,sha256=digest.hexdigest())
        return result,[info.volume,info.index_high,info.index_low]

    def describe(self,path):
        with self.guard(path,directory=path.is_dir(),read_only=True) as held:
            return held.signature,held.identity

    def copy_metadata(self,source,destination):
        # Owner/group/DACL differences must never be silently dropped. Lack of
        # permission to preserve these causes snapshot creation to fail closed.
        with self.guard(source,directory=source.is_dir(),read_only=True) as held:
            security=self.security_bytes(held.handle)
        control=self.w.WORD();revision=self.w.DWORD()
        self.check(self.security.GetSecurityDescriptorControl(security,c.byref(control),c.byref(revision)))
        flags=7 | (0x80000000 if control.value & 0x1000 else 0x20000000)
        self.check(self.security.SetFileSecurityW(self.path(destination),flags,security))
        attributes=source.lstat().st_file_attributes & (1|2|4|32)
        self.check(self.api.SetFileAttributesW(self.path(destination),attributes or 128))

    def copy_file(self,source,destination):
        self.check(self.api.CopyFileExW(self.path(source),self.path(destination),None,None,None,1))
        attributes=destination.lstat().st_file_attributes
        if attributes & 1:self.check(self.api.SetFileAttributesW(self.path(destination),(attributes & ~1) or 128))
        with destination.open('r+b') as stream:os.fsync(stream.fileno())
        self.copy_metadata(source,destination)

    def rename(self,held,destination):
        w=self.w
        class Rename(c.Structure):
            _fields_=[('flags',w.DWORD),('root',w.HANDLE),('length',w.DWORD),('name',w.WCHAR*1)]
        name=self.path(destination).encode('utf-16-le')
        buffer=c.create_string_buffer(c.sizeof(Rename)+len(name))
        value=Rename.from_buffer(buffer);value.flags=0;value.root=None;value.length=len(name)
        c.memmove(c.addressof(buffer)+Rename.name.offset,name,len(name))
        self.check(self.api.SetFileInformationByHandle(held.handle,3,buffer,len(buffer)))


class WindowsHandle:
    def __init__(self,owner,handle,signature,identity):
        self.owner,self.handle,self.directory,self.identity=owner,handle,signature['kind']=='dir',identity
    @property
    def signature(self):
        signature,identity=self.owner.inspect(self.handle,self.directory)
        if identity!=self.identity:raise RuntimeError('文件句柄身份发生变化')
        return signature
    def move(self,destination):self.owner.rename(self,destination)
