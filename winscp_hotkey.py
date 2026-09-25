"""Small diagnostic entry point for Alacritty's detached shortcut process."""
from datetime import datetime
import ctypes
import json
import os
from pathlib import Path
import sys
import traceback

LOG = Path(__file__).resolve().parent / 'runtime' / 'winscp-hotkey.log'


def log(event, **fields):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LOG.exists() and LOG.stat().st_size > 256_000:
        LOG.replace(LOG.with_suffix('.previous.log'))
    with LOG.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': datetime.now().isoformat(timespec='seconds'),
                                 'pid': os.getpid(), 'event': event, **fields}, ensure_ascii=False) + '\n')


if __name__ == '__main__':
    try:
        log('shortcut_started', parent=os.getppid())
        import ssh_integration
        result = ssh_integration.main()
        log('shortcut_finished', exit_code=result)
        sys.exit(result)
    except Exception as error:
        # Stack locations only: never log clipboard contents or credentials.
        log('shortcut_failed', error_type=type(error).__name__,
            frames=[{'file': Path(frame.filename).name, 'line': frame.lineno,
                     'function': frame.name} for frame in traceback.extract_tb(error.__traceback__)])
        ctypes.windll.user32.MessageBoxW(None, '快捷键执行失败。诊断已写入 ssh_config\\runtime\\winscp-hotkey.log。',
                                       'xxx — WinSCP', 0x10)
        sys.exit(1)
