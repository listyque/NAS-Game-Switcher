"""TACTIC route for the NAS connection page."""
from tactic.ui.common import BaseRefreshWdg
from ngs_launcher_plugin.connection import connection_panel


class ConnectionWdg(BaseRefreshWdg):
    def get_display(self):
        self.top.add(connection_panel())
        return self.top
