"""配置文件共用的私有原子写入流程。"""

import os
import tempfile
from pathlib import Path


def atomic_write(
    path: Path, content: bytes, *, prefix: str,
    check_target: bool = False, ignore_cleanup_errors: bool = False,
) -> None:
    """先同步临时文件再替换目标；目录准备和业务校验由调用方负责。"""

    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=prefix, suffix=".tmp")
        temporary = Path(name)
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # 密钥配置保留替换前的目标检查，不跟随链接写入。
        if check_target and (path.is_symlink() or (path.exists() and not path.is_file())):
            raise OSError("unsafe target")
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                if not ignore_cleanup_errors:
                    raise

