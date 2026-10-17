"""Metadata fingerprints, including macOS xattrs/ACLs absent from Python's os API."""
import ctypes
import errno
import hashlib
import os
import sys

if sys.platform=='darwin':
    _lib=ctypes.CDLL(None,use_errno=True)
    _lib.listxattr.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_int]
    _lib.listxattr.restype=ctypes.c_ssize_t
    _lib.getxattr.argtypes=[ctypes.c_char_p,ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_uint32,ctypes.c_int]
    _lib.getxattr.restype=ctypes.c_ssize_t
    _lib.acl_get_link_np.argtypes=[ctypes.c_char_p,ctypes.c_int]
    _lib.acl_get_link_np.restype=ctypes.c_void_p
    _lib.acl_to_text.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_ssize_t)]
    _lib.acl_to_text.restype=ctypes.c_void_p
    _lib.acl_free.argtypes=[ctypes.c_void_p]


def _error():
    number=ctypes.get_errno()
    raise OSError(number,os.strerror(number))


def signature(path,info):
    attributes=[];acl=''
    if sys.platform=='darwin':
        raw=os.fsencode(path)
        size=_lib.listxattr(raw,None,0,1)  # XATTR_NOFOLLOW
        if size<0:_error()
        names=ctypes.create_string_buffer(size)
        actual=_lib.listxattr(raw,names,size,1)
        if actual<0:_error()
        for name in sorted(filter(None,names.raw[:actual].split(b'\0'))):
            length=_lib.getxattr(raw,name,None,0,0,1)
            if length<0:_error()
            if length>64*1024*1024:raise ValueError('扩展属性过大，未提交')
            value=ctypes.create_string_buffer(length)
            actual=_lib.getxattr(raw,name,value,length,0,1)
            if actual<0:_error()
            attributes.append([name.hex(),hashlib.sha256(value.raw[:actual]).hexdigest()])
        pointer=_lib.acl_get_link_np(raw,0x100)  # ACL_TYPE_EXTENDED
        if not pointer:
            if ctypes.get_errno() not in (errno.ENOENT,errno.ENOTSUP):_error()
        else:
            text=None
            try:
                count=ctypes.c_ssize_t()
                text=_lib.acl_to_text(pointer,ctypes.byref(count))
                if not text:_error()
                acl=hashlib.sha256(ctypes.string_at(text,count.value)).hexdigest()
            finally:
                if text:_lib.acl_free(text)
                _lib.acl_free(pointer)
    elif hasattr(os,'listxattr'):
        for name in sorted(os.listxattr(path,follow_symlinks=False)):
            value=os.getxattr(path,name,follow_symlinks=False)
            attributes.append([os.fsencode(name).hex(),hashlib.sha256(value).hexdigest()])
    return [info.st_uid,info.st_gid,getattr(info,'st_flags',0),attributes,acl]
