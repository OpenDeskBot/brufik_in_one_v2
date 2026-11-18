#include "html.h"

const char index_html[] PROGMEM = R"rawliteral(
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>小歪配网</title>
  <style>
    :root{--bg:#e9e7de;--panel:#fff;--panel2:#f2f0e8;--ink:#16171b;--dim:#5b5b52;--line:#16171b;--accent:#ff6700;--bw:2px;--shadow:3px 3px 0 var(--line);--r:8px;font-family:ui-sans-serif,-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif}
    *{box-sizing:border-box}body{margin:0;min-height:100vh;background:var(--bg);color:var(--ink);background-image:linear-gradient(rgba(0,0,0,.06) 1px,transparent 1px),linear-gradient(90deg,rgba(0,0,0,.06) 1px,transparent 1px);background-size:28px 28px;padding:16px}
    .wrap{max-width:720px;margin:0 auto}.card{background:var(--panel);border:var(--bw) solid var(--line);border-radius:var(--r);box-shadow:var(--shadow);padding:14px;margin-bottom:12px}
    h1,h2{margin:0;font-size:20px}h3{font-size:14px;margin:0 0 6px}.section-title{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:10px}.actions{display:flex;gap:6px;flex-wrap:wrap}
    button{border:var(--bw) solid var(--line);border-radius:6px;background:var(--panel);box-shadow:2px 2px 0 var(--line);min-height:36px;padding:7px 10px;font-size:13px;font-weight:800;cursor:pointer;color:var(--ink)}button.primary{background:var(--accent);color:#fff}button:disabled{opacity:.6;cursor:not-allowed}button:focus-visible,input:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
    .device-id-row{display:flex;align-items:center;gap:10px}.device-id-value{flex:1;min-width:0}.field-label,label{display:block;font-weight:800;font-size:13px;margin-bottom:5px}#config-device-id{display:block;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:15px;overflow-wrap:anywhere}.device-id-row .actions{flex:none}.firmware{display:flex;align-items:center;gap:12px;margin:10px 0;font-size:13px}.firmware span{color:var(--dim)}.firmware b{overflow-wrap:anywhere}
    .field{margin-top:12px}.inline-form{display:grid;grid-template-columns:minmax(0,1fr) auto;align-items:center;gap:8px}.ap-offer{max-width:300px}.hint{color:var(--dim);font-size:12px;line-height:1.4;margin:6px 0 0}.reset-row{border-top:1px dashed var(--dim);padding-top:10px;margin-top:12px}.reset-row .hint{margin:6px 0 0}.reset-row button{flex:none}
    input{width:100%;min-width:0;border:var(--bw) solid var(--line);border-radius:6px;padding:8px 10px;background:#fff;font-size:16px;color:var(--ink)}.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:12px}.list{display:grid;gap:6px;margin-top:6px}.network{width:100%;min-width:0;display:flex;align-items:center;justify-content:space-between;gap:8px;text-align:left;background:var(--panel2);border:var(--bw) solid var(--line);border-radius:6px;padding:7px 9px;box-shadow:none}.network>span:first-child{min-width:0;overflow-wrap:anywhere}.network.on{background:var(--accent);color:#fff}.network small{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;font-weight:700}.network>button,.network .actions{flex-shrink:0}.network .server-address{flex:1 1 160px}.server-row{flex-wrap:wrap}.server-row .actions{margin-left:auto}.server-edit input{flex:1 1 180px}.server-edit .actions{margin-left:auto}#networks-list{max-height:176px;overflow-y:auto}#networks-list:empty{display:none}#networks-list small{flex-shrink:0}.wifi-saved{border-top:1px dashed var(--dim);padding-top:10px;margin-top:12px}
    .msg{border:var(--bw) solid var(--line);border-radius:6px;background:var(--panel2);padding:8px 10px;margin-top:10px;font-size:13px;line-height:1.45;overflow-wrap:anywhere}.msg.ok{background:#dff5df}.msg.err{background:#ffe1dc}.continue{margin-top:2px;padding-bottom:4px}#continue-boot-btn{width:100%;min-height:42px;font-size:15px}.spin{display:inline-block;width:14px;height:14px;border:3px solid rgba(0,0,0,.18);border-top-color:var(--line);border-radius:50%;animation:spin .8s linear infinite;margin-right:6px;vertical-align:-2px}.hidden{display:none}@keyframes spin{to{transform:rotate(360deg)}}@media(max-width:480px){body{padding:10px}.card{padding:12px}.device-id-row{gap:8px}.device-id-row .actions{gap:5px}.device-id-row button{padding:7px 8px}.form-grid{grid-template-columns:1fr}}
    .heading-group{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.heading-group h3{margin:0}.save-state{font-size:11px;color:var(--dim);background:var(--panel2);border-radius:4px;padding:3px 6px}.password-control{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px;align-items:center}.form-actions{grid-column:1/-1;display:flex;gap:8px}.form-actions .primary{flex:1}.form-note{grid-column:1/-1;margin:0}.choice{align-items:center;gap:6px;font-size:12px;font-weight:400;margin:6px 0 0}.choice:not(.hidden){display:flex}.choice input{width:auto;margin:0;accent-color:var(--accent)}.saved-network{flex-wrap:wrap}.saved-network>span:first-child{flex:1 1 180px}.saved-network .actions{margin-left:auto}.continue>.hint{text-align:center}
  </style>
</head>
<body>
  <main class="wrap">
    <section class="card" id="device-config-card" aria-labelledby="device-title">
      <div class="section-title"><h1 id="device-title">设备信息</h1></div>
      <div class="device-id-row">
        <div class="device-id-value"><span class="field-label">设备 ID</span><b id="config-device-id">读取中…</b></div>
        <div class="actions">
          <button type="button" id="copy-device-id-btn" onclick="copyDeviceId()" disabled>复制</button>
        </div>
      </div>
      <p class="hint">复制设备 ID 可用于绑定设备。</p>
      <div class="firmware"><span>固件版本</span><b id="firmware-version">读取中…</b></div>

      <div class="field">
        <label for="ap-offer-input">配网热点窗口（秒）</label>
        <div class="inline-form ap-offer">
          <input type="number" id="ap-offer-input" min="5" max="60" step="1" value="20" required>
          <button type="button" id="ap-offer-save-btn" onclick="saveApOfferSec()">保存</button>
        </div>
        <p class="hint" id="ap-offer-hint">5–60 秒，默认 20 秒；下次开机生效。</p>
      </div>

      <div class="field">
        <h3>云服务器配置</h3>
        <div id="ws-server-list" class="list"></div>
        <label for="ws-server-url-input" class="field">添加云服务器</label>
        <div class="inline-form">
          <input type="text" id="ws-server-url-input" autocapitalize="none" spellcheck="false" maxlength="511" placeholder="https://example.com/api/device/bootstrap">
          <button type="button" id="ws-server-add-btn" onclick="addWsServer()">添加</button>
        </div>
        <p class="hint">填写完整的 HTTP/HTTPS 启动接口地址，必须包含接口路径。连接和表情资源地址由服务器下发。</p>
      </div>

      <div class="reset-row">
        <div class="actions">
          <button type="button" id="reset-device-id-btn" onclick="resetDeviceId()">重置设备 ID</button>
          <button type="button" id="factory-reset-btn" onclick="factoryReset()">恢复出厂设置</button>
        </div>
        <p class="hint">生成新 ID 后需重新绑定；旧账号记录保留。</p>
        <p class="hint">恢复出厂将清除 Wi‑Fi、云服务器、绑定 PIN 与热点窗口设置并重启。</p>
      </div>
      <div id="config-message" class="msg hidden" role="status" aria-live="polite"></div>
    </section>

    <section class="card" id="wifi-settings-card" aria-labelledby="wifi-title">
      <div class="section-title">
        <h2 id="wifi-title">Wi‑Fi 设置</h2>
        <div class="actions">
          <button type="button" id="scan-btn" onclick="scanNetworks()"><span id="scan-spinner" class="spin hidden"></span><span id="scan-text">扫描网络</span></button>
          <button type="button" onclick="showManual()">隐藏网络</button>
        </div>
      </div>
      <div id="networks-list" class="list"></div>
      <form id="wifi-form" class="form-grid">
        <p id="wifi-edit-hint" class="hint form-note hidden">修改密码，保留原连接优先级。</p>
        <div>
          <label for="manual-ssid-input">Wi‑Fi 名称</label>
          <input type="text" id="manual-ssid-input" name="ssid" autocapitalize="none" spellcheck="false" placeholder="选择网络或手动输入">
        </div>
        <div>
          <label for="password-input">Wi‑Fi 密码</label>
          <div class="password-control">
            <input type="password" id="password-input" name="password" autocomplete="new-password" placeholder="开放网络可留空">
            <button type="button" id="toggle-password-btn" aria-label="显示密码" aria-controls="password-input" aria-pressed="false" onclick="togglePassword()">显示</button>
          </div>
          <label id="clear-password-label" class="choice hidden"><input type="checkbox" id="clear-password-input" onchange="updateWifiForm()">开放网络（清除原密码）</label>
        </div>
        <div class="form-actions">
          <button type="submit" id="save-btn" class="primary">保存 Wi‑Fi</button>
          <button type="button" id="cancel-wifi-edit-btn" class="hidden" onclick="cancelWifiPasswordEdit()">取消修改</button>
        </div>
      </form>
      <div class="wifi-saved">
        <div class="heading-group"><h3>已保存的 Wi‑Fi</h3><span id="wifi-save-state" class="save-state" role="status">读取中…</span></div>
        <p class="hint">按从上到下的顺序尝试连接；上移或下移后自动保存。</p>
        <div id="saved-wifi-list" class="list"></div>
        <p class="hint hidden" id="saved-wifi-empty">暂无已保存 Wi‑Fi。</p>
      </div>
      <div id="message" class="msg hidden" role="status" aria-live="polite"></div>
    </section>

    <div class="continue">
      <button type="button" id="continue-boot-btn" class="primary" onclick="continueBoot()">继续启动</button>
      <p class="hint">设备将关闭热点，并按已保存列表的顺序尝试连接。</p>
      <div id="boot-message" class="msg hidden" role="status" aria-live="polite"></div>
    </div>
  </main>

  <script>
    let savedWifiBusy = false;
    let savedWifiSsids = new Set();
    let editingWifiSsid = null;

    function setSavedWifiBusy(busy) {
      savedWifiBusy = busy;
      document.getElementById('save-btn').disabled = busy;
      document.getElementById('cancel-wifi-edit-btn').disabled = busy;
      document.getElementById('manual-ssid-input').disabled = busy;
      document.getElementById('clear-password-input').disabled = busy;
      updateWifiForm();
      document.querySelectorAll('#saved-wifi-list button').forEach(btn => {
        btn.disabled = busy || btn.dataset.boundary === 'true';
      });
    }

    function hidePassword() {
      document.getElementById('password-input').type = 'password';
      const btn = document.getElementById('toggle-password-btn');
      btn.textContent = '显示';
      btn.setAttribute('aria-label', '显示密码');
      btn.setAttribute('aria-pressed', 'false');
    }

    function togglePassword() {
      const input = document.getElementById('password-input');
      const visible = input.type === 'password';
      input.type = visible ? 'text' : 'password';
      const btn = document.getElementById('toggle-password-btn');
      btn.textContent = visible ? '隐藏' : '显示';
      btn.setAttribute('aria-label', visible ? '隐藏密码' : '显示密码');
      btn.setAttribute('aria-pressed', String(visible));
    }

    function updateWifiForm() {
      const ssid = document.getElementById('manual-ssid-input').value.trim();
      const saved = savedWifiSsids.has(ssid);
      const clearInput = document.getElementById('clear-password-input');
      if (!saved) clearInput.checked = false;
      document.getElementById('clear-password-label').classList.toggle('hidden', !saved);
      const password = document.getElementById('password-input');
      password.placeholder = clearInput.checked ? '开放网络无需密码' : (saved ? '留空保留原密码' : '开放网络可留空');
      password.disabled = savedWifiBusy || clearInput.checked;
      document.getElementById('toggle-password-btn').disabled = password.disabled;
      if (clearInput.checked) {
        password.value = '';
        hidePassword();
      }
      if (!savedWifiBusy) {
        document.getElementById('save-btn').textContent = editingWifiSsid === null ? '保存 Wi‑Fi' : '保存密码';
      }
    }

    function cancelWifiPasswordEdit() {
      if (savedWifiBusy) return;
      editingWifiSsid = null;
      const ssidInput = document.getElementById('manual-ssid-input');
      ssidInput.readOnly = false;
      ssidInput.value = '';
      document.getElementById('password-input').value = '';
      document.getElementById('clear-password-input').checked = false;
      document.getElementById('cancel-wifi-edit-btn').classList.add('hidden');
      document.getElementById('wifi-edit-hint').classList.add('hidden');
      document.querySelectorAll('#networks-list .network').forEach(item => item.classList.remove('on'));
      hidePassword();
      updateWifiForm();
    }

    function editSavedWifiPassword(ssid) {
      if (savedWifiBusy) return;
      cancelWifiPasswordEdit();
      editingWifiSsid = ssid;
      const ssidInput = document.getElementById('manual-ssid-input');
      ssidInput.value = ssid;
      ssidInput.readOnly = true;
      document.getElementById('cancel-wifi-edit-btn').classList.remove('hidden');
      document.getElementById('wifi-edit-hint').classList.remove('hidden');
      updateWifiForm();
      document.getElementById('password-input').focus();
    }

    function setMessage(text, type) {
      const el = document.getElementById('message');
      el.textContent = text;
      el.className = 'msg ' + (type || '');
      el.classList.remove('hidden');
    }

    function signalText(rssi) {
      if (rssi > -50) return '强';
      if (rssi > -70) return '优';
      if (rssi > -80) return '中';
      return '弱';
    }

    function scanNetworks() {
      const scanBtn = document.getElementById('scan-btn');
      const scanSpinner = document.getElementById('scan-spinner');
      const scanText = document.getElementById('scan-text');
      const networksList = document.getElementById('networks-list');

      scanSpinner.classList.remove('hidden');
      scanText.innerText = '扫描中...';
      scanBtn.disabled = true;
      networksList.innerHTML = '';
      document.getElementById('message').classList.add('hidden');

      fetch('/scan-wifi')
        .then(response => response.json())
        .then(data => {
          scanSpinner.classList.add('hidden');
          scanText.innerText = '扫描网络';
          scanBtn.disabled = false;

          if (data.length === 0) {
            setMessage('未找到网络。请靠近路由器后重新扫描，或手动输入隐藏网络。', 'err');
            return;
          }

          data.forEach(network => {
            const btn = document.createElement('button');
            btn.type = 'button';
            btn.className = 'network';
            btn.setAttribute('data-ssid', network.ssid);
            const name = document.createElement('span');
            name.textContent = network.ssid;
            const signal = document.createElement('small');
            signal.textContent = signalText(network.rssi) + ' · ' + network.rssi + ' dBm';
            btn.appendChild(name);
            btn.appendChild(signal);
            btn.classList.toggle('on', network.ssid === document.getElementById('manual-ssid-input').value);
            btn.addEventListener('click', () => selectNetwork(network.ssid));
            networksList.appendChild(btn);
          });
        })
        .catch(error => {
          scanSpinner.classList.add('hidden');
          scanText.innerText = '扫描网络';
          scanBtn.disabled = false;

          setMessage('扫描网络错误: ' + error.message, 'err');
        });
    }

    function selectNetwork(ssid) {
      if (savedWifiBusy) return;
      if (editingWifiSsid !== null) cancelWifiPasswordEdit();
      if (document.getElementById('manual-ssid-input').value !== ssid) {
        document.getElementById('password-input').value = '';
      }

      const networkItems = document.querySelectorAll('#networks-list .network');
      networkItems.forEach(item => {
        if (item.getAttribute('data-ssid') === ssid) {
          item.classList.add('on');
        } else {
          item.classList.remove('on');
        }
      });

      document.getElementById('manual-ssid-input').value = ssid;
      document.getElementById('clear-password-input').checked = false;
      hidePassword();
      updateWifiForm();
      document.getElementById('password-input').focus();
    }

    function showManual() {
      if (savedWifiBusy) return;
      cancelWifiPasswordEdit();
      document.getElementById('manual-ssid-input').focus();
    }

    document.getElementById('manual-ssid-input').addEventListener('input', function() {
      document.getElementById('clear-password-input').checked = false;
      hidePassword();
      updateWifiForm();
      document.querySelectorAll('#networks-list .network').forEach(item => {
        item.classList.toggle('on', item.getAttribute('data-ssid') === this.value);
      });
    });

    document.getElementById('wifi-form').addEventListener('submit', function(e) {
      e.preventDefault();

      const ssid = document.getElementById('manual-ssid-input').value.trim();
      const password = document.getElementById('password-input').value;
      const clearPassword = document.getElementById('clear-password-input').checked;
      const editing = editingWifiSsid !== null;
      const saveBtn = document.getElementById('save-btn');
      if (savedWifiBusy) return;

      if (!ssid) {
        setMessage('请选择一个网络，或手动输入 Wi‑Fi 名称。', 'err');
        return;
      }

      setSavedWifiBusy(true);
      saveBtn.textContent = '保存中...';
      setMessage('正在保存配置…', '');

      fetch(editing ? '/device-config/update-wifi-password' : '/save-wifi', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/x-www-form-urlencoded',
        },
        body: `ssid=${encodeURIComponent(ssid)}&password=${encodeURIComponent(password)}&clear_password=${clearPassword ? '1' : '0'}`
      })
      .then(response => response.json())
      .then(data => {
        if (data.success) {
          const result = clearPassword ? '已设为开放网络' : (savedWifiSsids.has(ssid) && !password ? '原密码已保留' : (editing ? '新密码已保存' : 'Wi‑Fi 已保存'));
          setMessage(result + '，待继续启动后连接。', 'ok');
          document.getElementById('password-input').value = '';
          hidePassword();
          return loadDeviceConfig();
        } else {
          setMessage('错误: ' + data.message, 'err');
        }
      })
      .catch(error => {
        setMessage('保存配置错误: ' + error.message, 'err');
      })
      .finally(() => {
        setSavedWifiBusy(false);
      });
    });

    function setConfigMessage(text, type) {
      const el = document.getElementById('config-message');
      el.textContent = text;
      el.className = 'msg ' + (type || '');
      el.classList.remove('hidden');
    }

    function updateDeviceId(id) {
      document.getElementById('config-device-id').textContent = id;
      const btn = document.getElementById('copy-device-id-btn');
      btn.disabled = false;
      btn.textContent = '复制';
    }

    async function copyDeviceId() {
      const id = document.getElementById('config-device-id').textContent;
      let copied = false;
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(id);
          copied = true;
        } catch (_) {
          // 热点页或内嵌浏览器可能不支持 Clipboard API，继续尝试传统复制。
        }
      }
      if (!copied) {
        const input = document.createElement('textarea');
        input.value = id;
        input.readOnly = true;
        input.style.cssText = 'position:fixed;left:-9999px;top:0;font-size:16px';
        document.body.appendChild(input);
        const previousFocus = document.activeElement;
        try {
          input.focus();
          input.select();
          input.setSelectionRange(0, id.length);
          copied = document.execCommand('copy');
        } catch (_) {
          copied = false;
        } finally {
          input.remove();
          if (previousFocus) previousFocus.focus({ preventScroll: true });
        }
      }
      if (copied) {
        document.getElementById('copy-device-id-btn').textContent = '已复制';
        setConfigMessage('设备 ID 已复制，可用于绑定设备。', 'ok');
      } else {
        setConfigMessage('复制失败，请长按或选中设备 ID 复制。', 'err');
      }
    }

    function loadDeviceConfig() {
      return fetch('/device-config')
        .then(r => r.json())
        .then(c => {
          if (!c.ok) throw new Error(c.message || '设备未返回配置');
          if (c.device_id) {
            updateDeviceId(c.device_id);
          }
          if (c.version) {
            document.getElementById('firmware-version').textContent = c.version;
          }
          const apInput = document.getElementById('ap-offer-input');
          if (typeof c.ap_offer_sec !== 'undefined') {
            apInput.value = c.ap_offer_sec;
          }
          if (typeof c.ap_offer_min !== 'undefined') {
            apInput.min = c.ap_offer_min;
          }
          if (typeof c.ap_offer_max !== 'undefined') {
            apInput.max = c.ap_offer_max;
          }
          if (typeof c.ap_offer_min !== 'undefined' && typeof c.ap_offer_max !== 'undefined') {
            document.getElementById('ap-offer-hint').textContent =
              c.ap_offer_min + '–' + c.ap_offer_max + ' 秒，默认 20 秒；下次开机生效。';
          }

          const listEl = document.getElementById('saved-wifi-list');
          const emptyEl = document.getElementById('saved-wifi-empty');
          listEl.innerHTML = '';
          const saved = c.saved_wifi || [];
          savedWifiSsids = new Set(saved);
          document.getElementById('wifi-save-state').textContent = saved.length ? '已保存，待连接' : '尚未保存网络';
          updateWifiForm();
          if (saved.length === 0) {
            emptyEl.classList.remove('hidden');
          } else {
            emptyEl.classList.add('hidden');
            saved.forEach((ssid, index) => {
              const row = document.createElement('div');
              row.className = 'network saved-network';
              row.style.cursor = 'default';
              const name = document.createElement('span');
              name.textContent = (index + 1) + '. ' + ssid;
              row.appendChild(name);
              const actions = document.createElement('span');
              actions.className = 'actions';
              const editBtn = document.createElement('button');
              editBtn.type = 'button';
              editBtn.textContent = '修改密码';
              editBtn.setAttribute('aria-label', '修改密码「' + ssid + '」');
              editBtn.disabled = savedWifiBusy;
              editBtn.onclick = () => editSavedWifiPassword(ssid);
              actions.appendChild(editBtn);
              ['up', 'down'].forEach(direction => {
                const moveBtn = document.createElement('button');
                moveBtn.type = 'button';
                moveBtn.textContent = direction === 'up' ? '上移' : '下移';
                moveBtn.setAttribute('aria-label', moveBtn.textContent + '「' + ssid + '」');
                const atBoundary = direction === 'up' ? index === 0 : index === saved.length - 1;
                moveBtn.dataset.boundary = String(atBoundary);
                moveBtn.disabled = savedWifiBusy || atBoundary;
                moveBtn.onclick = () => moveSavedWifi(ssid, direction);
                actions.appendChild(moveBtn);
              });
              const delBtn = document.createElement('button');
              delBtn.type = 'button';
              delBtn.textContent = '删除';
              delBtn.setAttribute('aria-label', '删除「' + ssid + '」');
              delBtn.disabled = savedWifiBusy;
              delBtn.onclick = () => deleteSavedWifi(ssid);
              actions.appendChild(delBtn);
              row.appendChild(actions);
              listEl.appendChild(row);
            });
          }

          renderWsServers(c);
        })
        .catch(err => setConfigMessage('读取设备配置失败，请刷新重试：' + err.message, 'err'));
    }

    function renderWsServers(c) {
      const listEl = document.getElementById('ws-server-list');
      listEl.innerHTML = '';
      const active = c.ws_active || 'builtin';
      const rows = [{ id: 'builtin', url: c.ws_builtin_url || '', label: '内置（默认）' }];
      (c.ws_servers || []).forEach(s => rows.push({ id: s.id, url: s.url, label: s.id }));

      rows.forEach(row => listEl.appendChild(buildWsServerRow(row, active)));
    }

    function buildWsServerRow(row, active) {
      const item = document.createElement('div');
      item.className = 'network server-row' + (row.id === active ? ' on' : '');
      item.style.cursor = 'default';
      const text = document.createElement('span');
      text.className = 'server-address';
      const label = document.createElement('b');
      label.textContent = row.label;
      const address = document.createElement('small');
      address.textContent = row.url;
      text.appendChild(label);
      text.appendChild(document.createElement('br'));
      text.appendChild(address);
      item.appendChild(text);

      const actions = document.createElement('span');
      actions.className = 'actions';

      const useBtn = document.createElement('button');
      useBtn.type = 'button';
      useBtn.textContent = row.id === active ? '当前' : '使用';
      useBtn.disabled = row.id === active;
      useBtn.onclick = () => selectWsServer(row.id);
      actions.appendChild(useBtn);

      if (row.id !== 'builtin') {
        const editBtn = document.createElement('button');
        editBtn.type = 'button';
        editBtn.textContent = '编辑';
        editBtn.onclick = () => editWsServerRow(item, row);
        actions.appendChild(editBtn);

        const delBtn = document.createElement('button');
        delBtn.type = 'button';
        delBtn.textContent = '删除';
        delBtn.onclick = () => deleteWsServer(row.id);
        actions.appendChild(delBtn);
      }

      item.appendChild(actions);
      return item;
    }

    function editWsServerRow(item, row) {
      item.innerHTML = '';
      item.className = 'network server-row server-edit';
      item.style.cursor = 'default';

      const input = document.createElement('input');
      input.type = 'text';
      input.value = row.url;
      input.setAttribute('aria-label', '云服务器地址');
      input.autocapitalize = 'none';
      input.spellcheck = false;

      const actions = document.createElement('span');
      actions.className = 'actions';

      const saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'primary';
      saveBtn.textContent = '保存';
      saveBtn.onclick = () => updateWsServer(row.id, input.value.trim(), saveBtn, input);

      const cancelBtn = document.createElement('button');
      cancelBtn.type = 'button';
      cancelBtn.textContent = '取消';
      cancelBtn.onclick = () => loadDeviceConfig();

      input.addEventListener('keydown', e => {
        if (e.key === 'Enter') {
          e.preventDefault();
          saveBtn.click();
        }
      });

      actions.appendChild(saveBtn);
      actions.appendChild(cancelBtn);
      item.appendChild(input);
      item.appendChild(actions);
      input.focus();
      input.select();
    }

    function updateWsServer(id, url, btn, input) {
      if (!url) {
        setConfigMessage('请输入云服务器地址。', 'err');
        input.focus();
        return;
      }
      btn.disabled = true;
      fetch('/device-config/ws-servers/update', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'id=' + encodeURIComponent(id) + '&url=' + encodeURIComponent(url)
      })
        .then(r => r.json())
        .then(data => {
          btn.disabled = false;
          if (data.success) {
            setConfigMessage('云服务器地址已更新，继续启动后生效。', 'ok');
            loadDeviceConfig();
          } else {
            setConfigMessage('更新失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => {
          btn.disabled = false;
          setConfigMessage('更新失败: ' + err.message, 'err');
        });
    }

    function addWsServer() {
      const input = document.getElementById('ws-server-url-input');
      const url = (input.value || '').trim();
      if (!url) {
        setConfigMessage('请输入云服务器地址。', 'err');
        return;
      }
      const btn = document.getElementById('ws-server-add-btn');
      btn.disabled = true;
      fetch('/device-config/ws-servers', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'url=' + encodeURIComponent(url)
      })
        .then(r => r.json())
        .then(data => {
          btn.disabled = false;
          if (data.success) {
            input.value = '';
            setConfigMessage('云服务器已添加。', 'ok');
            loadDeviceConfig();
          } else {
            setConfigMessage('添加失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => {
          btn.disabled = false;
          setConfigMessage('添加失败: ' + err.message, 'err');
        });
    }

    function selectWsServer(id) {
      fetch('/device-config/ws-servers/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'id=' + encodeURIComponent(id)
      })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            setConfigMessage('已切换云服务器，继续启动后生效。', 'ok');
            loadDeviceConfig();
          } else {
            setConfigMessage('切换失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => setConfigMessage('切换失败: ' + err.message, 'err'));
    }

    function deleteWsServer(id) {
      if (!confirm('确定删除该云服务器？')) return;
      fetch('/device-config/ws-servers/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'id=' + encodeURIComponent(id)
      })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            setConfigMessage('云服务器已删除。', 'ok');
            loadDeviceConfig();
          } else {
            setConfigMessage('删除失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => setConfigMessage('删除失败: ' + err.message, 'err'));
    }

    function saveApOfferSec() {
      const input = document.getElementById('ap-offer-input');
      if (!input.reportValidity()) return;
      const sec = Number(input.value);
      const btn = document.getElementById('ap-offer-save-btn');
      btn.disabled = true;
      fetch('/device-config/ap-offer-sec', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'sec=' + encodeURIComponent(sec)
      })
        .then(r => r.json())
        .then(data => {
          btn.disabled = false;
          if (data.success) {
            input.value = data.ap_offer_sec;
            setConfigMessage('配网热点窗口已保存为 ' + data.ap_offer_sec + ' 秒，下次开机生效。', 'ok');
          } else {
            setConfigMessage('保存失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => {
          btn.disabled = false;
          setConfigMessage('保存失败: ' + err.message, 'err');
        });
    }

    function resetDeviceId() {
      if (!confirm('重置后将生成新设备 ID，继续启动后需用新 ID 重新绑定。\n旧账号中的绑定和历史数据保留在旧 ID 下，不会自动迁移或解绑。\nWi‑Fi 和云服务器设置保留。确定重置？')) return;
      const btn = document.getElementById('reset-device-id-btn');
      btn.disabled = true;
      fetch('/device-config/reset-device-id', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
          if (data.success && data.device_id) {
            updateDeviceId(data.device_id);
            setConfigMessage('新设备 ID：' + data.device_id + '。继续启动后请用新 ID 重新绑定；旧账号记录保留。', 'ok');
          } else {
            setConfigMessage('重置失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => setConfigMessage('重置失败: ' + err.message, 'err'))
        .finally(() => { btn.disabled = false; });
    }

    function deleteSavedWifi(ssid) {
      if (savedWifiBusy) return;
      if (!confirm('确定删除已保存 Wi‑Fi「' + ssid + '」？')) return;
      setSavedWifiBusy(true);
      fetch('/device-config/delete-wifi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'ssid=' + encodeURIComponent(ssid)
      })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            setMessage('已删除 Wi‑Fi「' + ssid + '」', 'ok');
            return loadDeviceConfig();
          } else {
            setMessage('删除失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => setMessage('删除失败: ' + err.message, 'err'))
        .finally(() => setSavedWifiBusy(false));
    }

    function moveSavedWifi(ssid, direction) {
      if (savedWifiBusy) return;
      setSavedWifiBusy(true);
      fetch('/device-config/move-wifi', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'ssid=' + encodeURIComponent(ssid) + '&direction=' + encodeURIComponent(direction)
      })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            setMessage('Wi‑Fi 顺序已保存，将从最上面的网络开始尝试连接。', 'ok');
            return loadDeviceConfig();
          }
          setMessage('移动失败: ' + (data.message || '未知错误'), 'err');
        })
        .catch(err => setMessage('移动失败: ' + err.message, 'err'))
        .finally(() => setSavedWifiBusy(false));
    }

    function factoryReset() {
      if (!confirm('确定恢复出厂？将清除已保存 Wi‑Fi、云服务器、绑定 PIN 与热点窗口设置，设备随后重启。')) return;
      const btn = document.getElementById('factory-reset-btn');
      btn.disabled = true;
      fetch('/device-config/factory-reset', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            setConfigMessage('已恢复出厂，设备正在重启…', 'ok');
          } else {
            btn.disabled = false;
            setConfigMessage('恢复失败: ' + (data.message || '未知错误'), 'err');
          }
        })
        .catch(err => {
          btn.disabled = false;
          setConfigMessage('恢复失败: ' + err.message, 'err');
        });
    }

    function showContinueBootStarted() {
      const btn = document.getElementById('continue-boot-btn');
      btn.disabled = true;
      btn.textContent = '已继续启动';
      setBootMessage('设备正在启动，热点即将关闭，请查看设备屏幕上的连接结果。', 'ok');
    }

    function showContinueBootFailed(text) {
      const btn = document.getElementById('continue-boot-btn');
      btn.disabled = false;
      btn.textContent = '继续启动';
      setBootMessage(text, 'err');
    }

    function setBootMessage(text, type) {
      const el = document.getElementById('boot-message');
      el.textContent = text;
      el.className = 'msg ' + (type || '');
    }

    function continueBoot() {
      const btn = document.getElementById('continue-boot-btn');
      btn.disabled = true;
      btn.textContent = '正在继续启动…';
      fetch('/device-config/continue-boot', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
          if (data.success) {
            showContinueBootStarted();
          } else {
            showContinueBootFailed('操作失败: ' + (data.message || '未知错误'));
          }
        })
        .catch(() => {
          // 设备收到指令后立刻关闭热点，响应常来不及回到浏览器；此时的连接中断
          // 正是「已开始启动」的正常表现，不能报成失败。
          showContinueBootStarted();
        });
    }

    window.onload = function() {
      loadDeviceConfig();
      setTimeout(scanNetworks, 700);
    };
  </script>
</body>
</html>
)rawliteral";
