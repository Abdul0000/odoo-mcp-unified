// Fixed extension UI in an isolated shadow root; never changes page controls.
export function browserIndicator(phase) {
  let ui = globalThis.__odooMcpVisual;
  if (phase === 'clear') {ui?.dispose(); return;}
  if (!ui?.host.isConnected) {
    ui?.dispose();
    const host = document.createElement('div');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;';
    const shadow = host.attachShadow({mode: 'closed'});
    const frame = document.createElement('div');
    frame.style.cssText = 'position:absolute;inset:0;border:3px solid #8b5cf6;border-radius:7px;box-sizing:border-box;pointer-events:none;';
    const badge = document.createElement('span');
    badge.style.cssText = 'position:absolute;top:8px;right:12px;padding:7px 12px;background:#6d28d9;color:white;border-radius:14px;font:12px/1.4 system-ui,sans-serif;box-shadow:0 2px 10px #0003;pointer-events:none;';
    const target = document.createElement('div');
    target.style.cssText = 'display:none;position:fixed;border:3px solid #f59e0b;background:#f59e0b18;border-radius:5px;box-sizing:border-box;pointer-events:none;';
    shadow.append(frame, target, badge); document.documentElement.append(host);
    ui = {host, frame, badge, target, timer: null, targetTimer: null, node: null,
      update() {
        if (!this.node?.isConnected) {this.target.style.display = 'none'; return;}
        const rect = this.node.getBoundingClientRect();
        Object.assign(this.target.style, {display:'block', left:(rect.left - 3)+'px', top:(rect.top - 3)+'px', width:(rect.width + 6)+'px', height:(rect.height + 6)+'px'});
      },
      highlight(node) {
        this.node = node; this.update(); clearTimeout(this.targetTimer);
        this.targetTimer = setTimeout(() => {this.node = null; this.target.style.display = 'none';}, 1800);
      },
      dispose() {
        clearTimeout(this.timer); clearTimeout(this.targetTimer);
        window.removeEventListener('scroll', this.onMove, true); window.removeEventListener('resize', this.onMove);
        this.host.remove();
        if (globalThis.__odooMcpVisual === this) delete globalThis.__odooMcpVisual;
      },
    };
    ui.onMove = () => ui.update();
    window.addEventListener('scroll', ui.onMove, true); window.addEventListener('resize', ui.onMove);
    globalThis.__odooMcpVisual = ui;
  }
  ui.badge.textContent = phase === 'working' ? 'Odoo MCP · Working' : 'Odoo MCP · Tab controlled';
  ui.frame.style.borderColor = phase === 'working' ? '#8b5cf6' : '#a78bfa';
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => ui.dispose(), phase === 'working' ? 30000 : 8000);
}
