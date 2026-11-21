(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  // Every state-changing request carries this header; the server rejects POST/DELETE without it.
  const HDR = { 'X-Requested-With': 'ascii-forge' };
  async function api(path, { method = 'GET', body } = {}) {
    const opts = { method, headers: { ...HDR } };
    if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const res = await fetch(path, opts);
    let data = null;
    try { data = await res.json(); } catch (_) { /* not json */ }
    if (!res.ok) throw new Error((data && data.error) || `request failed (${res.status})`);
    return data;
  }

  const STUDIOS = {
    image: { label: 'image file', accept: 'image/*', outputs: ['png', 'txt'], defaults: ['png', 'txt'], cta: 'print it' },
    gif: { label: 'video file (or GIF)', accept: 'video/*,.gif', outputs: ['gif', 'frames_png', 'frames_txt'], defaults: ['gif'], cta: 'make GIF' },
    video: { label: 'video file', accept: 'video/*,.gif', outputs: ['mp4', 'gif', 'frames_png', 'frames_txt'], defaults: ['mp4'], cta: 'print video' },
  };
  const OUTPUT_LABELS = {
    png: 'PNG image', txt: 'text (.txt)', gif: 'animated GIF', mp4: 'MP4 video',
    frames_png: 'PNG frames (zip)', frames_txt: 'text frames (zip)',
  };

  let presets = null;
  let studio = 'image';
  let upload = null;          // { upload_id, name }
  let palette = ['#031a0a', '#1fa84f', '#8dffa8'];
  let pollTimer = null;
  let currentJob = null;

  // ------------------------------------------------------------ form helpers
  const RANGES = ['columns', 'glyph_scale', 'brightness', 'contrast', 'saturation'];
  const CHECKS = ['sort_chars', 'random_chars', 'invert', 'bold', 'waveform'];

  function syncOutputs() {
    RANGES.forEach((id) => {
      const v = parseFloat($(id).value);
      $(id + '-out').textContent = id === 'columns' ? `${v} columns` : (id === 'brightness' ? (v > 0 ? '+' : '') + v : v.toFixed(2).replace(/\.?0+$/, ''));
    });
  }

  function updateColorFields() {
    const mode = $('color_mode').value;
    $('mono-field').hidden = mode !== 'mono';
    $('palette-field').hidden = mode !== 'palette';
  }

  function renderPalette() {
    const box = $('palette-stops');
    box.innerHTML = '';
    palette.forEach((c, i) => {
      const inp = document.createElement('input');
      inp.type = 'color'; inp.value = c; inp.setAttribute('aria-label', `palette colour ${i + 1}`);
      inp.addEventListener('input', () => { palette[i] = inp.value; $('palette-preset').value = ''; drawPaletteBar(); });
      box.appendChild(inp);
    });
    $('stop-add').disabled = palette.length >= 6;
    $('stop-remove').disabled = palette.length <= 2;
    drawPaletteBar();
  }
  function drawPaletteBar() { $('palette-bar').style.background = `linear-gradient(90deg, ${palette.join(', ')})`; }

  function matchRamp(chars) {
    for (const [k, v] of Object.entries(presets.ramps)) if (v === chars) return k;
    return 'custom';
  }

  function applyStyle(name) {
    const s = presets.styles[name];
    if (!s) return;
    $('charset').value = s.charset;
    $('ramp').value = s.ramp || matchRamp(s.charset);
    $('color_mode').value = s.color_mode;
    if (s.fg) $('fg').value = s.fg;
    if (s.palette) { palette = [...s.palette]; $('palette-preset').value = presetNameFor(palette); renderPalette(); }
    $('bg').value = s.bg;
    $('random_chars').checked = !!s.random_chars;
    $('invert').checked = !!s.invert;
    updateColorFields();
  }
  function presetNameFor(p) {
    for (const [k, v] of Object.entries(presets.palettes)) if (v.join() === p.join()) return k;
    return '';
  }

  function buildOutputChecks() {
    const box = $('outputs');
    box.innerHTML = '';
    STUDIOS[studio].outputs.forEach((o) => {
      const l = document.createElement('label');
      l.className = 'check';
      l.innerHTML = `<input type="checkbox" data-output="${o}" ${STUDIOS[studio].defaults.includes(o) ? 'checked' : ''}> ${OUTPUT_LABELS[o]}`;
      box.appendChild(l);
    });
  }

  function setStudio(name) {
    studio = name;
    document.querySelectorAll('.tabs button').forEach((b) => b.setAttribute('aria-selected', String(b.dataset.tab === name)));
    const isProjects = name === 'projects';
    $('studio-view').hidden = isProjects;
    $('projects-view').hidden = !isProjects;
    if (isProjects) { loadProjects(); return; }
    const cfg = STUDIOS[name];
    $('file-label').textContent = cfg.label;
    $('file-input').accept = cfg.accept;
    $('submit-btn').textContent = cfg.cta;
    document.querySelectorAll('[data-only]').forEach((el) => { el.hidden = !el.dataset.only.split(' ').includes(name); });
    buildOutputChecks();
    clearUpload();
    $('result').hidden = true; $('preview-section').hidden = true;
  }

  function clearUpload() {
    upload = null; $('file-input').value = '';
    $('upload-status').textContent = 'or paste a link / local path below';
  }

  // ------------------------------------------------------------------ params
  function collect() {
    const p = { kind: studio, style: $('style').value, charset: $('charset').value,
      color_mode: $('color_mode').value, fg: $('fg').value, bg: $('bg').value, palette: [...palette],
      width: +$('width').value, compare: $('compare').value,
      gif_fps: +$('gif_fps').value, gif_width: +$('gif_width').value, frame_step: +$('frame_step').value,
      max_seconds: $('max-seconds').value, source: $('source-input').value.trim() };
    RANGES.forEach((id) => { p[id] = +$(id).value; });
    CHECKS.forEach((id) => { p[id] = $(id).checked; });
    p.outputs = [...document.querySelectorAll('[data-output]')].filter((c) => c.checked).map((c) => c.dataset.output);
    if (upload) { p.upload_id = upload.upload_id; p.source_name = upload.name; }
    return p;
  }

  function loadParams(p) {
    if (p.kind && STUDIOS[p.kind]) setStudio(p.kind);
    $('style').value = p.style || 'rgb';
    $('charset').value = p.charset || '';
    $('ramp').value = matchRamp($('charset').value);
    $('color_mode').value = p.color_mode || 'rgb';
    $('fg').value = p.fg || '#e6e6e6';
    $('bg').value = p.bg || '#000000';
    palette = Array.isArray(p.palette) && p.palette.length >= 2 ? [...p.palette] : palette;
    $('palette-preset').value = presetNameFor(palette);
    renderPalette();
    RANGES.forEach((id) => { if (p[id] !== undefined) $(id).value = p[id]; });
    CHECKS.forEach((id) => { $(id).checked = !!p[id]; });
    $('width').value = String(p.width || 1280);
    $('compare').value = p.compare || 'none';
    $('gif_fps').value = p.gif_fps || 12; $('gif_width').value = p.gif_width || 640; $('frame_step').value = p.frame_step || 10;
    $('max-seconds').value = p.max_seconds || '';
    $('source-input').value = /^https?:/i.test(p.source || '') ? p.source : '';
    document.querySelectorAll('[data-output]').forEach((c) => { c.checked = (p.outputs || []).includes(c.dataset.output); });
    syncOutputs(); updateColorFields();
  }

  // ----------------------------------------------------------------- uploads
  $('file-input').addEventListener('change', () => {
    const f = $('file-input').files[0];
    if (!f) { upload = null; return; }
    const fd = new FormData(); fd.append('file', f);
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');
    Object.entries(HDR).forEach(([k, v]) => xhr.setRequestHeader(k, v));
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) $('upload-status').textContent = `uploading ${Math.round(e.loaded / e.total * 100)}%...`; };
    xhr.onload = () => {
      let d = {}; try { d = JSON.parse(xhr.responseText); } catch (_) { /* ignore */ }
      if (xhr.status === 200) { upload = d; $('upload-status').textContent = `ready: ${d.name}`; }
      else { upload = null; $('upload-status').textContent = 'upload failed: ' + (d.error || xhr.status); }
    };
    xhr.onerror = () => { upload = null; $('upload-status').textContent = 'upload failed'; };
    $('upload-status').textContent = 'uploading...';
    xhr.send(fd);
  });

  // ----------------------------------------------------------------- preview
  $('preview-btn').addEventListener('click', async () => {
    const p = collect();
    if (!upload && !p.source) { showStatus('jammed: choose a file first'); return; }
    $('preview-btn').disabled = true;
    try {
      const res = await fetch('/preview', { method: 'POST', headers: { ...HDR, 'Content-Type': 'application/json' }, body: JSON.stringify(p) });
      if (!res.ok) { let d = {}; try { d = await res.json(); } catch (_) { /* ignore */ } throw new Error(d.error || 'preview failed'); }
      const blob = await res.blob();
      const img = $('preview-img');
      if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
      img.dataset.url = URL.createObjectURL(blob); img.src = img.dataset.url;
      $('preview-section').hidden = false;
      $('status-section').hidden = true;
    } catch (err) { showStatus('jammed: ' + err.message); }
    $('preview-btn').disabled = false;
  });

  // ----------------------------------------------------------------- convert
  function showStatus(msg, pct) {
    $('status-section').hidden = false;
    $('status-line').textContent = msg;
    if (pct !== undefined) $('bar-fill').style.width = pct + '%';
  }

  $('studio-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const p = collect();
    if (!p.upload_id && !p.source) { showStatus('jammed: choose a file or paste a link', 0); return; }
    if (!p.outputs.length) { showStatus('jammed: pick at least one export format', 0); return; }
    if (pollTimer) clearInterval(pollTimer);
    $('result').hidden = true;
    showStatus('sending to the printer...', 0);
    $('submit-btn').disabled = true; $('cancel-btn').hidden = false;
    try {
      const d = await api('/convert', { method: 'POST', body: p });
      currentJob = d.job_id;
      poll(d.job_id);
    } catch (err) { showStatus('jammed: ' + err.message); finishRun(); }
  });

  $('cancel-btn').addEventListener('click', () => { if (currentJob) api('/cancel/' + currentJob, { method: 'POST' }).catch(() => {}); });

  function finishRun() { $('submit-btn').disabled = false; $('cancel-btn').hidden = true; }

  function poll(jobId) {
    pollTimer = setInterval(async () => {
      let job;
      try { job = await api('/progress/' + jobId); } catch (err) {
        clearInterval(pollTimer); showStatus('jammed: ' + err.message); finishRun(); return;
      }
      showStatus(job.message || job.status);
      if (job.progress) $('bar-fill').style.width = Math.min(100, (job.progress[0] / Math.max(job.progress[1], 1)) * 100) + '%';
      if (job.status === 'done') {
        clearInterval(pollTimer); $('bar-fill').style.width = '100%'; finishRun(); renderResult(job);
      } else if (job.status === 'error' || job.status === 'cancelled') {
        clearInterval(pollTimer); showStatus((job.status === 'error' ? 'jammed: ' : '') + job.message); finishRun();
      }
    }, 700);
  }

  function renderResult(job) {
    const box = $('result');
    let html = '';
    if (job.slider) {
      html += `<div class="ba-label"><span>original</span><span>ASCII</span></div>
        <div class="ba"><img class="after" src="${esc(job.slider.after)}" alt="ASCII version">
        <img class="before" src="${esc(job.slider.before)}" alt="original"><div class="divider"></div>
        <input type="range" min="0" max="100" value="50" aria-label="before and after slider"></div>`;
    }
    (job.files || []).forEach((f) => {
      if (f.kind === 'mp4') html += `<div class="result-block"><video controls src="${esc(f.url)}"></video></div>`;
      else if (f.kind === 'gif') html += `<div class="result-block"><img src="${esc(f.url)}" alt="ASCII GIF"></div>`;
      else if (f.kind === 'png' && f.name === 'compare.png') html += `<div class="result-block"><img src="${esc(f.url)}" alt="before and after"></div>`;
    });
    html += '<div class="downloads">' + (job.files || []).map((f) =>
      `<a class="btn-secondary" href="${esc(f.url)}" download="${esc(f.name)}">download ${esc(f.label)} &middot; ${fmtSize(f.size)}</a>`).join('') + '</div>';
    html += `<div class="note">${esc(job.message && job.message !== 'done.' ? job.message : '')}</div>`;
    box.innerHTML = html; box.hidden = false;
    const slider = box.querySelector('.ba input');
    if (slider) {
      const before = box.querySelector('.ba .before'), div = box.querySelector('.ba .divider');
      slider.addEventListener('input', () => { before.style.clipPath = `inset(0 ${100 - slider.value}% 0 0)`; div.style.left = slider.value + '%'; });
    }
  }

  function fmtSize(n) {
    if (n > 1048576) return (n / 1048576).toFixed(1) + ' MB';
    if (n > 1024) return Math.round(n / 1024) + ' KB';
    return n + ' B';
  }
  function fmtBytes(n) { return fmtSize(n); }
  function fmtDate(ts) { return ts ? new Date(ts * 1000).toLocaleString() : ''; }

  // --------------------------------------------------------- project manager
  async function loadProjects() {
    try {
      const [pr, hi] = await Promise.all([api('/api/projects'), api('/api/history')]);
      renderProjects(pr.projects);
      renderHistory(hi);
    } catch (err) {
      $('history-list').innerHTML = `<div class="empty">could not load: ${esc(err.message)}</div>`;
    }
  }

  let cachedProjects = [], cachedJobs = [];

  function renderProjects(list) {
    cachedProjects = list;
    $('projects-list').innerHTML = list.length ? list.map((p) => `
      <div class="item">
        <div class="meta"><div class="title">${esc(p.name)}<span class="badge">${esc(p.kind)}</span></div>
        <div class="sub">saved ${esc(fmtDate(p.created))} &middot; ${esc(p.params.style)} &middot; ${p.params.columns} cols &middot; ${p.params.width}px</div></div>
        <div class="btns"><button class="btn-secondary" data-act="load-project" data-id="${p.id}">open</button>
        <button class="btn-danger" data-act="del-project" data-id="${p.id}">delete</button></div>
      </div>`).join('') : '<div class="empty">no saved projects yet &ndash; use "save as project" in any studio.</div>';
  }

  function renderHistory(data) {
    cachedJobs = data.jobs;
    const st = data.settings;
    $('retention').value = st.retention_hours; $('max-storage').value = st.max_storage_gb;
    $('storage-line').textContent = `generated files use ${fmtBytes(data.storage_used)} (cap ${st.max_storage_gb} GB). ` +
      (st.retention_hours > 0 ? `outputs are deleted ${st.retention_hours} h after they finish.` : 'time-based deletion is off.');
    $('history-list').innerHTML = data.jobs.length ? data.jobs.map((j) => {
      const status = j.expired ? '<span class="badge bad">files cleaned up</span>' :
        j.status === 'done' ? '<span class="badge ok">done</span>' : `<span class="badge bad">${esc(j.status)}</span>`;
      const pin = j.pinned ? '<span class="badge ok">pinned</span>' : '';
      const thumb = j.has_thumb ? `<img class="thumb" src="/thumb/${j.id}" alt="">` : '<div class="thumb-ph">no preview</div>';
      const open = j.status === 'done' && !j.expired ? `<button class="btn-secondary" data-act="open-job" data-id="${j.id}">open</button>` : '';
      const pinBtn = j.status === 'done' && !j.expired ? `<button class="btn-secondary" data-act="pin-job" data-id="${j.id}" data-pinned="${j.pinned ? 1 : 0}">${j.pinned ? 'unpin' : 'pin'}</button>` : '';
      return `<div class="item">${thumb}
        <div class="meta"><div class="title">${esc(j.label)}<span class="badge">${esc(j.kind)}</span>${status}${pin}</div>
        <div class="sub">${esc(fmtDate(j.created))}${j.size ? ' &middot; ' + fmtBytes(j.size) : ''}${j.status === 'error' ? ' &middot; ' + esc(j.message) : ''}</div></div>
        <div class="btns">${open}<button class="btn-secondary" data-act="reuse-job" data-id="${j.id}">re-use settings</button>${pinBtn}
        <button class="btn-danger" data-act="del-job" data-id="${j.id}">delete</button></div></div>`;
    }).join('') : '<div class="empty">nothing converted yet.</div>';
  }

  $('projects-view').addEventListener('click', async (e) => {
    const b = e.target.closest('button[data-act]');
    if (!b) return;
    const { act, id } = b.dataset;
    try {
      if (act === 'load-project') { loadParams(cachedProjects.find((p) => String(p.id) === id).params); }
      else if (act === 'del-project') { await api('/api/projects/' + id, { method: 'DELETE' }); loadProjects(); }
      else if (act === 'reuse-job') { loadParams(cachedJobs.find((j) => j.id === id).params); }
      else if (act === 'pin-job') { await api(`/api/history/${id}/pin`, { method: 'POST', body: { pinned: b.dataset.pinned !== '1' } }); loadProjects(); }
      else if (act === 'del-job') { if (confirm('Delete this conversion and its files?')) { await api('/api/history/' + id, { method: 'DELETE' }); loadProjects(); } }
      else if (act === 'open-job') {
        const j = cachedJobs.find((x) => x.id === id);
        loadParams(j.params);
        const job = { files: j.files, message: j.message };
        if (j.kind === 'image') job.slider = { before: `/media/${id}/slider_before.png`, after: `/media/${id}/slider_after.png` };
        renderResult(job);
      }
    } catch (err) { alert(err.message); }
  });

  $('settings-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/settings', { method: 'POST', body: { retention_hours: $('retention').value, max_storage_gb: $('max-storage').value } });
      loadProjects();
    } catch (err) { alert(err.message); }
  });
  $('cleanup-now').addEventListener('click', async () => {
    try { const d = await api('/api/cleanup', { method: 'POST' }); alert(`removed ${d.cleaned.expired_jobs} expired conversion(s), ${d.cleaned.orphans} stray folder(s), ${d.cleaned.uploads} old upload(s)`); loadProjects(); }
    catch (err) { alert(err.message); }
  });

  $('save-project-btn').addEventListener('click', async () => {
    const name = prompt('Name this project:', `${studio} - ${$('style').selectedOptions[0].textContent}`);
    if (name === null) return;
    try { await api('/api/projects', { method: 'POST', body: { name, params: collect() } }); showStatus(`saved project "${name}" (settings only - files are not stored)`, 0); }
    catch (err) { showStatus('jammed: ' + err.message); }
  });

  // -------------------------------------------------------------------- init
  document.querySelectorAll('.tabs button').forEach((b) => b.addEventListener('click', () => setStudio(b.dataset.tab)));
  RANGES.forEach((id) => $(id).addEventListener('input', syncOutputs));
  $('color_mode').addEventListener('change', updateColorFields);
  $('style').addEventListener('change', () => applyStyle($('style').value));
  $('ramp').addEventListener('change', () => { if ($('ramp').value !== 'custom') $('charset').value = presets.ramps[$('ramp').value]; });
  $('charset').addEventListener('input', () => { $('ramp').value = matchRamp($('charset').value); });
  $('palette-preset').addEventListener('change', () => {
    const v = presets.palettes[$('palette-preset').value];
    if (v) { palette = [...v]; renderPalette(); }
  });
  $('stop-add').addEventListener('click', () => { if (palette.length < 6) { palette.push(palette[palette.length - 1]); $('palette-preset').value = ''; renderPalette(); } });
  $('stop-remove').addEventListener('click', () => { if (palette.length > 2) { palette.pop(); $('palette-preset').value = ''; renderPalette(); } });

  (async function init() {
    presets = await api('/api/presets');
    $('style').innerHTML = Object.entries(presets.styles).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
    $('ramp').innerHTML = [...Object.keys(presets.ramps), 'custom'].map((k) => `<option value="${k}">${k}</option>`).join('');
    $('palette-preset').innerHTML = '<option value="">custom</option>' +
      Object.keys(presets.palettes).map((k) => `<option value="${esc(k)}">${esc(k)}</option>`).join('');
    $('style').value = 'rgb';
    applyStyle('rgb');
    syncOutputs();
    renderPalette();
    setStudio('image');
  })();
})();
