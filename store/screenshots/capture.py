"""Capture Odoo screens with headless Chrome over --remote-debugging-pipe (stdlib only).

usage: python3 -I capture.py OUT_DIR BASE_URL 'name=path[|WxH[@scale]][|js]' ...

The store set (A=/odoo/action-tk_pushnotification):
  "main_screenshot=$A.tk_push_message_action|1280x590@1.5|document.querySelector('.o_facet_remove').click()"
  "screenshot_notification=$A.tk_push_notification_action/4|1280x800@1.5"
  "screenshot_provider=$A.tk_push_provider_action/2|1280x650@1.5"
  "screenshot_devices=$A.tk_push_device_action|1280x430@1.5"
  "screenshot_topics=$A.tk_push_topic_action|1280x310@1.5"
"""
import base64, json, os, subprocess, sys, tempfile, time

CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
W, H = 1440, 900

out_dir, base, *shots = sys.argv[1:]
to_chrome_r, to_chrome_w = os.pipe()
from_chrome_r, from_chrome_w = os.pipe()

def child_fds():
    r, w = os.dup(to_chrome_r), os.dup(from_chrome_w)  # out of the way of 3 and 4 first
    os.dup2(r, 3)
    os.dup2(w, 4)
    os.set_inheritable(3, True)
    os.set_inheritable(4, True)

profile = tempfile.mkdtemp(prefix='shots-profile-')
proc = subprocess.Popen(
    [CHROME, '--headless=new', '--remote-debugging-pipe', f'--user-data-dir={profile}',
     '--no-first-run', '--hide-scrollbars', f'--window-size={W},{H}', 'about:blank'],
    preexec_fn=child_fds, close_fds=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
os.close(to_chrome_r); os.close(from_chrome_w)
reader = os.fdopen(from_chrome_r, 'rb', buffering=0)
seq = 0
buf = b''

def call(method, session=None, **params):
    global seq, buf
    seq += 1
    msg = {'id': seq, 'method': method, 'params': params}
    if session:
        msg['sessionId'] = session
    os.write(to_chrome_w, json.dumps(msg).encode() + b'\0')
    while True:
        while b'\0' not in buf:
            chunk = reader.read(1 << 16)
            if not chunk:
                raise RuntimeError('chrome closed the pipe')
            buf += chunk
        raw, buf = buf.split(b'\0', 1)
        reply = json.loads(raw)
        if reply.get('id') == seq:
            if 'error' in reply:
                raise RuntimeError(f'{method}: {reply["error"]}')
            return reply.get('result', {})

def js(session, expr):
    return call('Runtime.evaluate', session, expression=expr, returnByValue=True).get('result', {}).get('value')

def wait_idle(session, timeout=15):
    deadline = time.time() + timeout
    time.sleep(1)
    while time.time() < deadline:
        ready = js(session, "document.readyState === 'complete' && !!document.querySelector('.o_action_manager .o_view_controller, .o_form_view, .o_list_view') && !document.querySelector('.o_loading_indicator')")
        if ready:
            time.sleep(1.5)  # let fonts, avatars and animations settle
            return
        time.sleep(0.5)

try:
    target = call('Target.createTarget', url='about:blank')['targetId']
    session = call('Target.attachToTarget', targetId=target, flatten=True)['sessionId']
    call('Page.enable', session)
    call('Runtime.enable', session)
    call('Emulation.setDeviceMetricsOverride', session, width=W, height=H, deviceScaleFactor=1, mobile=False)

    call('Page.navigate', session, url=f'{base}/web/login')
    time.sleep(3)
    js(session, """
        document.querySelector('input[name=login]').value = 'admin';
        document.querySelector('input[name=password]').value = 'admin';
        document.querySelector('form.oe_login_form, form').submit();
    """)
    time.sleep(4)

    for spec in shots:
        # name=path[|WxH[@scale]][|js to run before the capture]
        name, rest = spec.split('=', 1)
        path, size, script = (rest.split('|', 2) + ['', ''])[:3]
        w, h, scale = W, H, 1
        if size:
            dims, _, sc = size.partition('@')
            w, h = map(int, dims.split('x'))
            scale = float(sc or 1)
        call('Emulation.setDeviceMetricsOverride', session, width=w, height=h, deviceScaleFactor=scale, mobile=False)
        call('Page.navigate', session, url=f'{base}{path}')
        wait_idle(session)
        if script:
            res = js(session, script)
            if res is not None: print("js:", res)
            wait_idle(session)
        data = call('Page.captureScreenshot', session, format='png')['data']
        with open(os.path.join(out_dir, f'{name}.png'), 'wb') as fh:
            fh.write(base64.b64decode(data))
        print('saved', name, js(session, 'location.pathname'))
finally:
    proc.terminate()
