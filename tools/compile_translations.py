"""Compile the checked-in Russian catalog with the pinned Qt build tools."""
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET


def compile_catalog(root):
    import PySide6
    source=root/'translations/ngs_ru.ts'
    messages=ET.parse(source).findall('.//message')
    for message in messages:
        english=message.findtext('source') or ''
        translated=message.findtext('translation') or ''
        if not translated or message.find('translation').get('type')=='unfinished':
            raise ValueError('Unfinished translation: '+english)
        if sorted(re.findall(r'\{\d+\}|%\d+',english))!=sorted(re.findall(r'\{\d+\}|%\d+',translated)):
            raise ValueError('Translation placeholders differ: '+english)
    output=root/'assets/i18n/ngs_ru.qm';output.parent.mkdir(parents=True,exist_ok=True)
    executable=Path(PySide6.__file__).parent/('lrelease.exe' if __import__('os').name=='nt' else 'lrelease')
    subprocess.run([str(executable),str(source),'-qm',str(output)],check=True)
    return len(messages)


if __name__=='__main__':compile_catalog(Path(__file__).resolve().parents[1])
