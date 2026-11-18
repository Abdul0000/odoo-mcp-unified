// Fixed extension UI in an isolated shadow root; never changes page controls.
export function browserIndicator(phase) {
  let ui = globalThis.__odooMcpVisual;
  if (phase === 'clear') {ui?.dispose(); return;}
  if (!ui?.host.isConnected) {
    ui?.dispose();
    const host = document.createElement('div');
    host.setAttribute('aria-hidden', 'true');
    host.style.cssText = 'position:fixed!important;inset:0!important;z-index:2147483647!important;pointer-events:none!important;overflow:hidden!important;';
    const shadow = host.attachShadow({mode: 'closed'});
    const frame = document.createElement('div');
    frame.style.cssText = 'position:absolute;inset:0;border:3px solid #8b5cf6;border-radius:7px;box-sizing:border-box;pointer-events:none;';
    const badge = document.createElement('span');
    badge.style.cssText = 'position:absolute;top:8px;right:12px;padding:7px 12px;background:#6d28d9;color:white;border-radius:14px;font:12px/1.4 system-ui,sans-serif;box-shadow:0 2px 10px #0003;pointer-events:none;';
    const target = document.createElement('div');
    target.style.cssText = 'display:none;position:fixed;border:3px solid #f59e0b;background:#f59e0b18;border-radius:5px;box-sizing:border-box;pointer-events:none;';
    const cursor = document.createElement('div');
    cursor.style.cssText = 'position:fixed;left:0;top:0;pointer-events:none;transition:transform 120ms linear;filter:drop-shadow(1px 1px 1px #fff) drop-shadow(0 1px 3px #0005);';
    const arrow = document.createElement('div');
    arrow.style.cssText = 'width:22px;height:29px;background:#7c3aed;clip-path:polygon(0 0,0 91%,28% 71%,46% 100%,65% 89%,47% 62%,88% 62%);pointer-events:none;';
    const cursorLabel = document.createElement('span');
    cursorLabel.style.cssText = 'position:absolute;left:22px;top:18px;padding:3px 7px;background:#6d28d9;color:white;border-radius:9px;white-space:nowrap;font:11px/1.3 system-ui,sans-serif;pointer-events:none;';
    const ripple = document.createElement('div');
    ripple.style.cssText = 'position:absolute;left:-15px;top:-15px;width:30px;height:30px;border:2px solid #f59e0b;border-radius:50%;box-sizing:border-box;opacity:0;pointer-events:none;';
    cursor.append(ripple, arrow, cursorLabel);
    shadow.append(frame, target, badge, cursor); document.documentElement.append(host);
    ui = {host, frame, badge, target, cursor, cursorLabel, ripple, pulse: null, timer: null, targetTimer: null, node: null,
      update() {
        if (!this.node?.isConnected) {this.target.style.display = 'none'; return;}
        const rect = this.node.getBoundingClientRect();
        Object.assign(this.target.style, {display:'block', left:(rect.left - 3)+'px', top:(rect.top - 3)+'px', width:(rect.width + 6)+'px', height:(rect.height + 6)+'px'});
        this.move(rect.left + rect.width / 2, rect.top + rect.height / 2);
      },
      move(x, y, action) {
        const width = window.innerWidth || 1024, height = window.innerHeight || 768;
        this.cursor.style.transform = `translate(${Math.max(4, Math.min(width - 28, x))}px, ${Math.max(4, Math.min(height - 32, y))}px)`;
        this.cursorLabel.style.left = x > width - 150 ? '-110px' : '22px';
        this.cursorLabel.style.top = y > height - 60 ? '-24px' : '18px';
        if (action?.startsWith('Scroll')) {this.node = null; this.target.style.display = 'none';}
        if (action) this.cursorLabel.textContent = 'MCP · ' + action;
      },
      highlight(node, action = 'Target') {
        this.node = node; this.update(); this.cursorLabel.textContent = 'MCP · ' + action;
        this.pulse?.cancel();
        if (action === 'click' || action === 'press') this.pulse = this.ripple.animate?.([
          {opacity:0.9, transform:'scale(0.3)'}, {opacity:0, transform:'scale(1.8)'},
        ], {duration:650, easing:'ease-out'});
        clearTimeout(this.targetTimer);
        this.targetTimer = setTimeout(() => {this.node = null; this.target.style.display = 'none';}, 1800);
      },
      dispose() {
        clearTimeout(this.timer); clearTimeout(this.targetTimer); this.pulse?.cancel();
        window.removeEventListener('scroll', this.onMove, true); window.removeEventListener('resize', this.onMove);
        this.host.remove();
        if (globalThis.__odooMcpVisual === this) delete globalThis.__odooMcpVisual;
      },
    };
    ui.onMove = () => ui.update();
    window.addEventListener('scroll', ui.onMove, true); window.addEventListener('resize', ui.onMove);
    globalThis.__odooMcpVisual = ui;
    ui.move((window.innerWidth || 1024) - 140, 85, 'Ready');
  }
  if (phase === 'working') ui.cursorLabel.textContent = 'MCP · Working';
  ui.badge.textContent = phase === 'working' ? 'Odoo MCP · Working' : 'Odoo MCP · Tab controlled';
  ui.frame.style.borderColor = phase === 'working' ? '#8b5cf6' : '#a78bfa';
  clearTimeout(ui.timer);
  ui.timer = setTimeout(() => ui.dispose(), phase === 'working' ? 30000 : 8000);
}
