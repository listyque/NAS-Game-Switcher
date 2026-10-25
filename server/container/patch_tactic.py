"""TACTIC 5.0.a02 loses its project on the first worker password login."""
from pathlib import Path

path = Path('/opt/tactic/tactic/src/pyasm/security/security.py')
source = path.read_text()
before = '''        except:
            Project.set_project(current_project)


    requires_2fa = classmethod(requires_2fa)'''
after = '''        except:
            pass
        finally:
            Project.set_project(current_project)


    requires_2fa = classmethod(requires_2fa)'''
if source.count(before) != 1:
    raise RuntimeError('Unexpected TACTIC security source; review patch before upgrading')
path.write_text(source.replace(before, after))
