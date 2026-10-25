"""Administrator-only local enrollment; never exposed as an anonymous API."""

def pair_device(name):
    import json, secrets
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search, SearchType
    if not Environment.get_security().is_admin() or Project.get().get_code() != 'ngs_launcher':
        raise PermissionError('NGS administrator required')
    if not isinstance(name,str) or not 1 <= len(name.strip()) <= 160:
        raise ValueError('Device name required')
    device, secret = secrets.token_hex(16), secrets.token_hex(32)
    query=Search('ngs_launcher/catalog_config');query.add_filter('code','service');service=query.get_sobject()
    if not service:raise ValueError('NGS plugin is not configured')
    value=json.loads(service.get_value('data'))
    obj=SearchType.create('ngs_launcher/api_device')
    for key,item in dict(code=device,secret=secret,name=name.strip(),enabled=True,state='{}').items(): obj.set_value(key,item)
    obj.commit()
    value['devices']=sorted(set(value.get('devices',[]))|{device})
    service.set_value('data',json.dumps(value));service.commit()
    query=Search('ngs_launcher/catalog_config');query.add_filter('code','authority');authority=query.get_sobject()
    public=json.loads(authority.get_value('data'))['public'] if authority else None
    return dict(device=device,secret=secret,public=public)


def revoke_device(device):
    import re
    from pyasm.biz import Project
    from pyasm.common import Environment
    from pyasm.search import Search
    if not Environment.get_security().is_admin() or Project.get().get_code() != 'ngs_launcher' or not re.fullmatch('[0-9a-f]{32}',device):
        raise PermissionError('NGS administrator required')
    query=Search('ngs_launcher/api_device');query.add_filter('code',device);obj=query.get_sobject()
    if obj:obj.set_value('enabled',False);obj.commit()
    return dict(revoked=True)
