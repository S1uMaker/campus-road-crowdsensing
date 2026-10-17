function esc(s) {
  return String(s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function badgeHtml(b) {
  return '<span class="badge' + (b.got ? ' got' : '') + '" title="'
    + esc(b.desc) + '">' + b.icon + ' ' + esc(b.name) + '</span>';
}

function render(d) {
  const board = document.getElementById('board');
  document.getElementById('sub').textContent = '更新于 ' + (d.generated || '—');
  const riders = d.riders || [];
  if (!riders.length) {
    board.innerHTML = '<div class="empty">暂时还没有骑手上榜——'
      + '完成一次采集就能出现在这里！</div>';
    return;
  }
  const max = Math.max.apply(null, riders.map(r => r.points).concat([1]));
  board.innerHTML = riders.map((r, i) => {
    const rank = i + 1;
    const medal = rank === 1 ? '🥇' : rank === 2 ? '🥈' : rank === 3 ? '🥉' : rank;
    const km = (r.dist_m / 1000).toFixed(2);
    return '<div class="rider">'
      + '<div class="rank' + (rank <= 3 ? ' r' + rank : '') + '">' + medal + '</div>'
      + '<div class="who">'
      +   '<div class="name">' + esc(r.rider) + '</div>'
      +   '<div class="meta">数据 ' + r.samples.toLocaleString() + ' 条 · 里程 '
      +     km + ' km · ' + r.rides + ' 趟</div>'
      +   '<div class="badges">' + (r.badges || []).map(badgeHtml).join('') + '</div>'
      +   '<div class="bar"><i style="width:'
      +     Math.round(r.points / max * 100) + '%"></i></div>'
      + '</div>'
      + '<div class="pts">'
      +   '<div><span class="num">' + r.points + '</span><span class="unit"> 分</span></div>'
      +   '<div class="bd">数据 ' + r.breakdown.data + ' · 里程 '
      +     r.breakdown.dist + ' · 出勤 ' + r.breakdown.attend + '</div>'
      + '</div>'
      + '</div>';
  }).join('');
}

function load() {
  fetch('/api/points').then(r => r.json()).then(render).catch(() => {
    document.getElementById('sub').textContent = '暂时连接不上服务器，稍后会自动重试…';
  });
}
load();
setInterval(load, 10000);
