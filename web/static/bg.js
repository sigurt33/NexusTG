// Подложка: бродячая нейронная сеть из частиц и связей.
(function(){
  const canvas = document.createElement('canvas');
  canvas.id = 'neural-bg';
  document.body.insertBefore(canvas, document.body.firstChild);
  const ctx = canvas.getContext('2d');

  let W = 0, H = 0, dpr = window.devicePixelRatio || 1;
  function resize() {
    W = window.innerWidth;
    H = window.innerHeight;
    canvas.width = W * dpr;
    canvas.height = H * dpr;
    canvas.style.width = W + 'px';
    canvas.style.height = H + 'px';
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  resize();
  window.addEventListener('resize', resize);

  const N = Math.min(220, Math.round((W * H) / 8000) + 150);
  const LINK_DIST = 120;

  const pts = Array.from({length: N}, () => ({
    x: Math.random() * W,
    y: Math.random() * H,
    vx: (Math.random() - 0.5) * 0.9,
    vy: (Math.random() - 0.5) * 0.9,
    r: 1.2 + Math.random() * 1.5,
  }));

  function themeColor() {
    const t = document.documentElement.getAttribute('data-theme');
    return t === 'dark'
      ? {dot: 'rgba(160,200,255,0.55)', line: 'rgba(140,180,255,'}
      : {dot: 'rgba(60,90,160,0.45)',  line: 'rgba(60,90,160,'};
  }

  function step() {
    ctx.clearRect(0, 0, W, H);
    const c = themeColor();
    // движение
    for (const p of pts) {
      p.x += p.vx;
      p.y += p.vy;
      if (p.x < 0 || p.x > W) p.vx *= -1;
      if (p.y < 0 || p.y > H) p.vy *= -1;
    }
    // связи
    for (let i = 0; i < pts.length; i++) {
      for (let j = i + 1; j < pts.length; j++) {
        const a = pts[i], b = pts[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx * dx + dy * dy;
        if (d2 < LINK_DIST * LINK_DIST) {
          const d = Math.sqrt(d2);
          const alpha = (1 - d / LINK_DIST) * 0.5;
          ctx.strokeStyle = c.line + alpha.toFixed(3) + ')';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    // точки
    ctx.fillStyle = c.dot;
    for (const p of pts) {
      ctx.beginPath();
      ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(step);
  }
  step();
})();
