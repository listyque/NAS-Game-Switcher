"""Qt translations shared by the launcher and its protected input component."""
from PySide6.QtCore import QCoreApplication, QLocale, QTranslator


def language(value):
    return value if value in ('en', 'ru') else 'en'


def tr(text, comment=None):
    return QCoreApplication.translate('NGS', text, comment)


def configure(storage, selected=None):
    app = QCoreApplication.instance()
    if app is None:
        raise RuntimeError('Create the Qt application before installing translations')
    selected = language(storage.read_settings().get('language') if selected is None else selected)
    catalog = storage.resources / 'assets/i18n/ngs_ru.qm'
    identity = (selected, str(catalog))
    if getattr(app, '_ngs_translation', None) == identity:
        return selected
    translator = QTranslator(app)
    if selected == 'ru' and not translator.load(str(catalog)):
        raise RuntimeError('The Russian translation catalog is missing or invalid')
    previous = getattr(app, '_ngs_translator', None)
    if previous is not None:
        app.removeTranslator(previous)
        previous.deleteLater()
    if selected == 'ru':
        app.installTranslator(translator)
    app._ngs_translator = translator
    app._ngs_translation = identity
    QLocale.setDefault(QLocale('ru_RU' if selected == 'ru' else 'en_US'))
    return selected
