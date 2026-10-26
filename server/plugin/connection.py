"""Public enrollment data, shown only inside the authenticated NAS administration."""


def connection_panel():
    import hashlib
    from pathlib import Path
    from pyasm.biz import Project
    from pyasm.common import Environment
    if not Environment.get_security().is_admin() or Project.get().get_code() != 'ngs_launcher':
        raise PermissionError('NGS administrator required')
    path = Path(Environment.get_asset_dir()) / 'ngs_launcher/server-public-key.bin'
    with path.open('rb') as stream:
        public = stream.read(284)
    if len(public) != 283 or public[:4] != b'RSA1':
        raise ValueError('Invalid NGS server public key')
    fingerprint = hashlib.sha256(public).hexdigest()
    return '''<div style="padding:24px;max-width:800px">
<h2>Connect NGS / Подключение NGS</h2>
<p>Copy this link into the server address field in NGS. Review the server, then sign in or create an account.<br>
Скопируйте ссылку в поле адреса сервера NGS. Подтвердите сервер, затем войдите или создайте аккаунт.</p>
<p>The link contains only the server address and public key fingerprint. Share it through a trusted channel.<br>
Ссылка содержит только адрес и публичный отпечаток ключа. Передайте её по доверенному каналу.</p>
<label>Server address / Адрес сервера
<input class="ngs-connect-address" style="display:block;width:100%;margin:12px 0" type="url"
placeholder="https://your-nas" onfocus="if(!this.value)this.value=location.origin;"></label>
<p>Use HTTPS for internet access. Local HTTP requires this pairing link.<br>
Для доступа через интернет нужен HTTPS. Для локального HTTP используйте эту ссылку сопряжения.</p>
<button type="button" onclick="
var panel=this.parentNode,address=panel.querySelector('.ngs-connect-address'),link=panel.querySelector('.ngs-connect-link');
var value=address.value.trim()||location.origin;
try{var server=new URL(value);if(!/^https?:$/.test(server.protocol)||server.username||server.password||server.search||server.hash||!/^\\/(tactic\\/?)?$/.test(server.pathname))throw Error();
link.value=server.origin+'/tactic/ngs_launcher/#ngs-key=FINGERPRINT';
link.focus();link.select();
if(navigator.clipboard){if(isSecureContext){navigator.clipboard.writeText(link.value).catch(function(){link.focus();link.select();});return;}}
document.execCommand('copy');
}catch(e){link.value='Enter a valid HTTP(S) server address / Введите корректный адрес HTTP(S)';}">
Copy connection link / Скопировать ссылку</button>
<input class="ngs-connect-link" style="display:block;width:100%;margin:12px 0" readonly
aria-label="NGS connection link / Ссылка подключения NGS">
<p>SHA-256: <code>FINGERPRINT</code></p>
</div>'''.replace('FINGERPRINT', fingerprint)
