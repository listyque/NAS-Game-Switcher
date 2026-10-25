from pathlib import Path
from pyasm.common import Environment
from ngs_launcher_plugin.prepare import activate_plugin
activate_plugin(Path(Environment.get_plugin_dir()) / 'ngs_launcher_plugin')
