// Motion is a progressive enhancement: everything works if GSAP is missing,
// and all of it is skipped when the OS asks for reduced motion.
const MOTION = !!window.gsap && !window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// tabs
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  if (t.classList.contains('active')) return;
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  const panel = document.getElementById('panel-' + t.dataset.panel);
  panel.classList.add('active');
  t.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  if (MOTION) gsap.fromTo(panel, { y: 8, opacity: 0 }, { y: 0, opacity: 1, duration: .34, ease: 'power2.out' });
}));

// spring press feedback on every button (pointer only; CSS covers :active too)
if (MOTION) {
  document.addEventListener('pointerdown', e => {
    const b = e.target.closest('button.go, button.mini, .tile, .tab');
    if (b) gsap.to(b, { scale: .975, duration: .1, ease: 'power2.out' });
  });
  ['pointerup', 'pointercancel', 'pointerleave'].forEach(ev =>
    document.addEventListener(ev, e => {
      const b = e.target.closest('button.go, button.mini, .tile, .tab');
      if (b) gsap.to(b, { scale: 1, duration: .45, ease: 'elastic.out(1, .55)' });
    }));
}

// Cascade newly rendered rows/tiles instead of popping them all in at once.
// Never run while the tab is hidden: rAF is paused there, so the "from" state
// (opacity 0) would stick and the content would be invisible. clearProps also
// removes the inline styles afterwards so nothing is left half-applied.
function revealChildren(container, selector) {
  if (!MOTION || !container || document.hidden) return;
  const items = container.querySelectorAll(selector);
  if (!items.length) return;
  gsap.from(items, {
    y: 6, opacity: 0, duration: .3, ease: 'power2.out',
    stagger: Math.min(.03, .5 / items.length), clearProps: 'opacity,transform',
  });
}

// safety net: if the tab was hidden mid-animation, make sure nothing stays invisible
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && window.gsap) {
    document.querySelectorAll('.tile, .filerow, .thumb, #bx-list .row').forEach(el => {
      if (parseFloat(getComputedStyle(el).opacity) < 1) gsap.set(el, { clearProps: 'opacity,transform' });
    });
  }
});

function setStatus(id, cls, html) {
  const el = document.getElementById(id);
  el.className = 'status show ' + cls;
  el.innerHTML = html;
}

function doneLink(statusId, jobId, label) {
  setStatus(statusId, 'done', '&#10004; Done — <a href="/api/file/' + jobId + '">Download ' + label + '</a>');
}

function poll(jobId, statusId, btn, onDone) {
  let misses = 0;   // tolerate brief network blips (common on phones / Tailscale)
  const timer = setInterval(async () => {
    try {
      const r = await fetch('/api/status/' + jobId);
      const j = await r.json();
      misses = 0;
      if (j.status === 'running') {
        setStatus(statusId, 'working', '&#9203; ' + j.progress);
      } else {
        clearInterval(timer);
        btn.disabled = false;
        if (j.status === 'done') {
          if (onDone) { setStatus(statusId, 'done', '&#10004; Done'); onDone(jobId, j); }
          else doneLink(statusId, jobId, j.filename || 'file');
          if (j.warning) document.getElementById(statusId).innerHTML += '<br>&#9888; ' + j.warning;
        } else setStatus(statusId, 'error', '&#10008; ' + (j.error || 'Unknown error'));
      }
    } catch (e) {
      if (++misses < 20) { setStatus(statusId, 'working', '&#9203; reconnecting to server...'); return; }
      clearInterval(timer); btn.disabled = false;
      setStatus(statusId, 'error', 'Lost connection to server. The job may still finish — reopen the page later.');
    }
  }, 1000);
}

// ---- "From this PC" picker: browse files already on the PC, no upload ----
const bx = { path: '', kind: '', multi: true, sel: new Map(), onPick: null, offset: 0, total: 0,
             view: (() => { try { return localStorage.getItem('bxView') || 'medium'; } catch { return 'medium'; } })() };

function openBrowser({ kind = '', multi = true, title = 'Choose from this PC', onPick }) {
  Object.assign(bx, { kind, multi, onPick, sel: new Map(), offset: 0 });
  document.getElementById('bx-title').textContent = title;
  document.getElementById('bx-search').value = '';
  document.querySelectorAll('#bx-views button').forEach(b => b.classList.toggle('on', b.dataset.view === bx.view));
  document.getElementById('browser').classList.add('open');
  loadBrowser('');
}

async function loadBrowser(path, append = false) {
  bx.path = path;
  if (!append) bx.offset = 0;
  const q = document.getElementById('bx-search').value.trim();
  const url = `/api/browse?path=${encodeURIComponent(path)}&kind=${bx.kind}`
    + `&q=${encodeURIComponent(q)}&offset=${bx.offset}&limit=100`;
  const list = document.getElementById('bx-list');
  if (!append) list.innerHTML = '<div class="row"><span class="nm">Loading...</span></div>';
  let j;
  try { j = await (await fetch(url)).json(); }
  catch { list.innerHTML = '<div class="row"><span class="nm">Could not reach the server</span></div>'; return; }
  if (j.error) { list.innerHTML = `<div class="row"><span class="nm">${j.error}</span></div>`; return; }

  bx.total = j.total;
  document.getElementById('bx-crumbs').textContent = path ? path.replace(/\//g, ' / ') : 'Folders on this PC';

  // folders and Back are always plain rows; files follow the chosen view
  let head = '';
  if (j.parent !== null) head += `<div class="row" data-dir="${j.parent}"><span class="ic">&#8617;</span><span class="nm">Back</span></div>`;
  j.dirs.forEach(d => {
    head += `<div class="row" data-dir="${d.path}"><span class="ic">&#128193;</span><span class="nm">${d.name}</span></div>`;
  });

  const icons = { video: '&#127909;', image: '&#128247;', audio: '&#127925;', file: '&#128196;' };
  let body = '';
  if (bx.view === 'list') {
    j.files.forEach(f => {
      body += `<div class="row${bx.sel.has(f.path) ? ' sel' : ''}" data-file="${f.path}">`
        + `<span class="ic">${bx.sel.has(f.path) ? '&#10003;' : icons[f.kind] || icons.file}</span>`
        + `<span class="nm">${f.name}</span><span class="sz">${f.mb} MB</span></div>`;
    });
  } else {
    j.files.forEach(f => {
      const canPreview = (f.kind === 'video' || f.kind === 'image');
      const inner = !canPreview
        ? `<div class="ph">${icons[f.kind] || icons.file}</div>`
        : f.cloud
          ? `<div class="ph">${icons[f.kind]}<div class="cloud">&#9729;&#65039;<span>Tap to preview</span></div></div>`
          : `<div class="ph" data-thumb="${f.path}">${icons[f.kind]}</div>`;
      body += `<div class="tile${bx.sel.has(f.path) ? ' sel' : ''}" data-file="${f.path}">`
        + `<span class="tick">&#10003;</span>${inner}`
        + `<span class="badge">${f.mb} MB</span><div class="cap">${f.name}</div></div>`;
    });
  }
  if (!j.dirs.length && !j.files.length) body += '<div class="row"><span class="nm">Nothing here</span></div>';

  const wrap = bx.view === 'list' ? body : `<div class="grid ${bx.view}">${body}</div>`;
  if (append && bx.view !== 'list') {
    list.querySelector('.grid').insertAdjacentHTML('beforeend', body);
  } else if (append) {
    list.insertAdjacentHTML('beforeend', body);
  } else {
    list.innerHTML = head + wrap;
  }
  loadThumbs();
  revealChildren(list, '.tile, .row');
  const shown = j.offset + j.files.length;
  document.getElementById('bx-more').style.display = shown < j.total ? '' : 'none';
  document.getElementById('bx-more').dataset.next = shown;
  updateBrowserCount();
}

// fetch thumbnails only as tiles scroll into view, so opening a folder of 2,700
// videos doesn't ask the PC for 2,700 previews at once
let thumbObserver;
function loadThumbs() {
  if (!thumbObserver) {
    thumbObserver = new IntersectionObserver((entries, obs) => {
      entries.forEach(en => {
        if (!en.isIntersecting) return;
        const ph = en.target, path = ph.dataset.thumb;
        obs.unobserve(ph);
        const img = new Image();
        img.onload = () => ph.replaceWith(img);
        img.onerror = () => { ph.innerHTML = ph.innerHTML || '&#128196;'; };
        img.src = '/api/thumb?path=' + encodeURIComponent(path);
      });
    }, { root: document.getElementById('bx-list'), rootMargin: '300px' });
  }
  document.querySelectorAll('#bx-list .ph[data-thumb]').forEach(ph => thumbObserver.observe(ph));
}

document.getElementById('bx-views').addEventListener('click', e => {
  const b = e.target.closest('button');
  if (!b) return;
  bx.view = b.dataset.view;
  try { localStorage.setItem('bxView', bx.view); } catch {}
  document.querySelectorAll('#bx-views button').forEach(x => x.classList.toggle('on', x === b));
  loadBrowser(bx.path);
});

function updateBrowserCount() {
  const n = bx.sel.size;
  document.getElementById('bx-count').textContent =
    (n ? `${n} selected` : 'Tap a file to select') + (bx.total ? ` · ${bx.total} files here` : '');
}

document.getElementById('bx-list').addEventListener('click', async e => {
  const row = e.target.closest('.row, .tile');
  if (!row) return;
  if (row.dataset.dir !== undefined) { loadBrowser(row.dataset.dir); return; }
  const path = row.dataset.file;
  if (!path) return;

  // tapping the cloud badge downloads that one file from iCloud, with live progress
  const cloud = e.target.closest('.cloud');
  if (cloud) {
    if (cloud.dataset.busy) return;
    cloud.dataset.busy = '1';
    cloud.innerHTML = '<div class="spinner"></div><span class="eta">…</span>'
      + '<span class="cancel-x">&times;</span>';
    let jobId = null;
    cloud.querySelector('.cancel-x').addEventListener('click', ev => {
      ev.stopPropagation();
      if (jobId) fetch('/api/thumb_cancel/' + jobId, { method: 'POST' });
    });
    try {
      const r = await (await fetch('/api/thumb_start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path })
      })).json();
      if (r.error) throw new Error(r.error);
      jobId = r.job_id;
      const timer = setInterval(async () => {
        let s;
        try { s = await (await fetch('/api/status/' + jobId)).json(); }
        catch { return; }                       // ride out a blip, keep polling
        if (s.status === 'running') {
          cloud.querySelector('.eta').textContent = s.progress;
        } else {
          clearInterval(timer);
          delete cloud.dataset.busy;
          if (s.status === 'done') {
            const img = new Image();
            img.onload = () => cloud.parentElement.replaceWith(img);
            img.src = '/api/thumb?force=1&path=' + encodeURIComponent(path);
          } else {
            cloud.innerHTML = `&#9888;<span>${s.error === 'cancelled' ? 'Cancelled' : 'Preview failed'}</span>`;
          }
        }
      }, 700);
    } catch (err) {
      delete cloud.dataset.busy;
      cloud.innerHTML = '&#9888;<span>Could not start</span>';
    }
    return;
  }

  const name = (row.querySelector('.nm') || row.querySelector('.cap')).textContent;
  const mark = (el, on) => {
    el.classList.toggle('sel', on);
    const ic = el.querySelector('.ic');
    if (ic) ic.innerHTML = on ? '&#10003;' : '&#128196;';
  };
  if (bx.sel.has(path)) { bx.sel.delete(path); mark(row, false); }
  else {
    if (!bx.multi) {
      bx.sel.clear();
      document.querySelectorAll('#bx-list .sel').forEach(r => mark(r, false));
    }
    bx.sel.set(path, name);
    mark(row, true);
  }
  updateBrowserCount();
});
document.getElementById('bx-more').addEventListener('click', e => { bx.offset = +e.target.dataset.next; loadBrowser(bx.path, true); });
document.getElementById('bx-search').addEventListener('input', () => { clearTimeout(bx.t); bx.t = setTimeout(() => loadBrowser(bx.path), 300); });
document.getElementById('bx-close').addEventListener('click', () => document.getElementById('browser').classList.remove('open'));
document.getElementById('browser').addEventListener('click', e => { if (e.target.id === 'browser') e.currentTarget.classList.remove('open'); });
document.getElementById('bx-use').addEventListener('click', () => {
  document.getElementById('browser').classList.remove('open');
  if (bx.onPick) bx.onPick([...bx.sel.entries()].map(([path, name]) => ({ path, name })));
});

// Adds an "Or choose from this PC" button under a drop zone. Picked files are
// remembered on the input element and sent as paths instead of being uploaded.
function addPcPicker(dropId, inputId, kind, textId) {
  const drop = document.getElementById(dropId), input = document.getElementById(inputId);
  const row = document.createElement('div');
  row.className = 'pick-row';
  row.innerHTML = `<button class="mini">&#128187; Choose from this PC (no upload)</button>`
    + `<span class="or">— instant for files already here, e.g. iCloud Photos</span>`;
  drop.after(row);
  row.querySelector('button').addEventListener('click', () => openBrowser({
    kind, multi: input.multiple, title: 'Choose from this PC',
    onPick(items) {
      if (!items.length) return;
      input.value = '';                       // clear any browser-picked upload
      input.serverFiles = items;
      const label = items.length === 1 ? items[0].name : `${items.length} files from this PC`;
      if (textId) document.getElementById(textId).textContent = label;
      const panel = drop.closest('fieldset, .panel');
      const btn = panel.querySelector('button.go'), status = panel.querySelector('.status');
      if (btn && status) setStatus(status.id, 'working', `&#10003; ${label} — tap <b>${btn.textContent}</b> to start`);
    }
  }));
}

// include PC-picked files in a submission; returns true if anything was added
function appendServerFiles(input, fd) {
  if (!input.serverFiles || !input.serverFiles.length) return false;
  fd.append('server_files', JSON.stringify(input.serverFiles.map(f => f.path)));
  return true;
}

function wireDrop(dropId, inputId, onFiles) {
  const drop = document.getElementById(dropId), input = document.getElementById(inputId);
  // input.click() fires a click that bubbles back up to the drop zone; without this
  // guard the picker is opened twice, and iOS Safari then drops the chosen files.
  drop.addEventListener('click', e => { if (e.target !== input) input.click(); });
  input.addEventListener('change', () => {
    input.serverFiles = null;      // an upload replaces any earlier PC selection
    onFiles(input.files);
    // tell the user what happens next: picking files doesn't start the upload by itself
    const panel = drop.closest('fieldset, .panel') || drop.parentElement;
    const btn = panel.querySelector('button.go');
    const status = panel.querySelector('.status');
    if (input.files.length && btn && status) {
      const n = input.files.length;
      setStatus(status.id, 'working', `&#10003; ${n} file${n > 1 ? 's' : ''} ready — tap <b>${btn.textContent}</b> to start`);
    }
  });
  drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('over'); });
  drop.addEventListener('dragleave', () => drop.classList.remove('over'));
  drop.addEventListener('drop', e => {
    e.preventDefault(); drop.classList.remove('over');
    input.files = e.dataTransfer.files;
    onFiles(input.files);
  });
}

// POST a FormData with live upload progress (fetch can't report upload progress).
// Resolves with the parsed JSON reply; rejects with a readable message on failure.
function uploadForm(url, fd, statusId) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const t0 = Date.now();
    xhr.open('POST', url);
    xhr.upload.onprogress = e => {
      if (!e.lengthComputable) return;
      const pct = Math.floor(e.loaded / e.total * 100);
      const mb = n => (n / 1048576).toFixed(1);
      const secs = (Date.now() - t0) / 1000;
      const speed = secs > 0.5 ? ` at ${(e.loaded / 1048576 / secs).toFixed(2)} MB/s` : '';
      setStatus(statusId, 'working', pct < 100
        ? `&#9203; uploading ${pct}% (${mb(e.loaded)} / ${mb(e.total)} MB${speed})`
        : '&#9203; upload complete, processing...');
    };
    xhr.onload = () => {
      try { resolve(JSON.parse(xhr.responseText)); }
      catch { reject(`Server error (HTTP ${xhr.status})`); }
    };
    xhr.onerror = () => reject('Upload failed — connection to the server was lost. '
      + 'If you are on another device, check that Tailscale is connected on both.');
    xhr.onabort = () => reject('Upload was cancelled');
    xhr.send(fd);
  });
}

// generic instant POST (multipart) -> direct done/error
async function postForm(url, fd, statusId, btn, label) {
  btn.disabled = true;
  setStatus(statusId, 'working', '&#9203; uploading...');
  try {
    const j = await uploadForm(url, fd, statusId);
    btn.disabled = false;
    if (j.error) { setStatus(statusId, 'error', '&#10008; ' + j.error); return; }
    doneLink(statusId, j.job_id, label);
  } catch (e) { btn.disabled = false; setStatus(statusId, 'error', '&#10008; ' + e); }
}

// upload, then poll a background job until it finishes
// onDone(jobId, statusJson) runs on success, for jobs that render their own results
async function uploadAndPoll(url, fd, statusId, btn, onDone) {
  btn.disabled = true;
  setStatus(statusId, 'working', '&#9203; uploading...');
  try {
    const j = await uploadForm(url, fd, statusId);
    if (j.error) { btn.disabled = false; setStatus(statusId, 'error', '&#10008; ' + j.error); return; }
    poll(j.job_id, statusId, btn, onDone);
  } catch (e) { btn.disabled = false; setStatus(statusId, 'error', '&#10008; ' + e); }
}

// --- YouTube ---
document.getElementById('yt-go').addEventListener('click', async function () {
  const url = document.getElementById('yt-url').value.trim();
  if (!url) { setStatus('yt-status', 'error', 'Please paste a URL'); return; }
  const mode = document.querySelector('input[name=yt-mode]:checked').value;
  const cookies = document.getElementById('yt-cookies').value;
  this.disabled = true;
  setStatus('yt-status', 'working', '&#9203; starting...');
  try {
    const r = await fetch('/api/youtube', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({url, mode, cookies}) });
    const j = await r.json();
    if (j.error) { this.disabled = false; setStatus('yt-status', 'error', j.error); return; }
    poll(j.job_id, 'yt-status', this);
  } catch (e) { this.disabled = false; setStatus('yt-status', 'error', '&#10008; Lost connection to server'); }
});

// --- A/V Convert ---
// "song.mp3" for one file, "3 files (1.2 GB): a.mov, b.mov, c.mov" for several
function describeFiles(files) {
  if (!files.length) return '';
  if (files.length === 1) return files[0].name;
  const total = [...files].reduce((s, f) => s + f.size, 0);
  const size = total >= 1073741824 ? (total / 1073741824).toFixed(2) + ' GB' : (total / 1048576).toFixed(1) + ' MB';
  return `${files.length} files (${size}): ` + [...files].map(f => f.name).join(', ');
}

wireDrop('conv-drop', 'conv-file', files => {
  if (files.length) document.getElementById('conv-drop-text').textContent = describeFiles(files);
});
document.getElementById('conv-go').addEventListener('click', async function () {
  const input = document.getElementById('conv-file');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('conv-status', 'error', 'Choose a file first'); return; }
  const fd = new FormData();
  [...input.files].forEach(f => fd.append('files', f));
  appendServerFiles(input, fd);
  fd.append('target', document.getElementById('conv-target').value);
  fd.append('gif_fps', document.getElementById('gif-fps').value);
  fd.append('gif_width', document.getElementById('gif-width').value);
  uploadAndPoll('/api/convert', fd, 'conv-status', this);
});

// GIF has its own settings; show them only when GIF is the target
document.getElementById('conv-target').addEventListener('change', e => {
  document.getElementById('conv-gif-opts').style.display = e.target.value === 'gif' ? '' : 'none';
});

// --- Vocal / instrumental split ---
wireDrop('split-drop', 'split-file', files => {
  if (files.length) document.getElementById('split-drop-text').textContent = describeFiles(files);
});
document.getElementById('split-go').addEventListener('click', async function () {
  const input = document.getElementById('split-file');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('split-status', 'error', 'Choose an audio/video file first'); return; }
  const fd = new FormData();
  [...input.files].forEach(f => fd.append('files', f));
  appendServerFiles(input, fd);
  fd.append('format', document.querySelector('input[name=split-fmt]:checked').value);
  uploadAndPoll('/api/separate', fd, 'split-status', this);
});

// --- Split for sharing ---
wireDrop('cut-drop', 'cut-file', files => {
  if (files.length) document.getElementById('cut-drop-text').textContent = describeFiles(files);
});
document.querySelectorAll('input[name=cut-mode]').forEach(r => r.addEventListener('change', () => {
  const bySize = document.querySelector('input[name=cut-mode]:checked').value === 'size';
  document.getElementById('cut-size-box').style.display = bySize ? '' : 'none';
  document.getElementById('cut-dur-box').style.display = bySize ? 'none' : '';
}));
document.getElementById('cut-preset').addEventListener('change', e => {
  const custom = e.target.value === 'custom';
  const mb = document.getElementById('cut-mb');
  mb.style.display = custom ? '' : 'none';
  if (!custom) mb.value = e.target.value;
});

document.getElementById('cut-go').addEventListener('click', function () {
  const input = document.getElementById('cut-file');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('cut-status', 'error', 'Choose a video first'); return; }
  const fd = new FormData();
  [...input.files].forEach(f => fd.append('files', f));
  appendServerFiles(input, fd);
  fd.append('mode', document.querySelector('input[name=cut-mode]:checked').value);
  fd.append('limit_mb', document.getElementById('cut-mb').value || 2000);
  fd.append('clip_minutes', document.getElementById('cut-minutes').value || 10);
  document.getElementById('cut-parts').innerHTML = '';
  uploadAndPoll('/api/split', fd, 'cut-status', this, renderParts);
});

// one download link per clip — downloading a ZIP of 2 GB parts would be pointless
function renderParts(jobId, j) {
  if (!j.parts || !j.parts.length) return;
  const total = j.parts.reduce((s, p) => s + p.mb, 0);
  let html = `<p class="hint">${j.parts.length} clip${j.parts.length > 1 ? 's' : ''}, `
    + `${total >= 1024 ? (total / 1024).toFixed(2) + ' GB' : total.toFixed(0) + ' MB'} total — `
    + `download each one and send it separately.</p><table class="comp-table">`;
  j.parts.forEach((p, i) => {
    html += `<tr><td class="fname" title="${p.name}">${p.name}</td><td>${p.mb} MB</td>`
      + `<td><a href="/api/part/${jobId}/${i}" download>Download</a></td></tr>`;
  });
  document.getElementById('cut-parts').innerHTML = html + '</table>';
  revealChildren(document.getElementById('cut-parts'), 'tr');
}

// "From this PC" buttons: pick files already on the PC instead of uploading
addPcPicker('conv-drop', 'conv-file', 'video,audio', 'conv-drop-text');
addPcPicker('split-drop', 'split-file', 'video,audio', 'split-drop-text');
addPcPicker('cut-drop', 'cut-file', 'video', 'cut-drop-text');
addPcPicker('imgconv-drop', 'imgconv-files', 'image');
addPcPicker('imgcomp-drop', 'imgcomp-files', 'image');
addPcPicker('merge-drop', 'merge-files', 'pdf');
addPcPicker('p2i-drop', 'p2i-file', 'pdf', 'p2i-drop-text');

// --- Image format conversion ---
wireDrop('imgconv-drop', 'imgconv-files', files => {
  document.getElementById('imgconv-list').textContent = [...files].map(f => f.name).join(', ');
});
document.getElementById('imgconv-go').addEventListener('click', function () {
  const input = document.getElementById('imgconv-files');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('imgconv-status', 'error', 'Choose images first'); return; }
  const fd = new FormData();
  [...input.files].forEach(f => fd.append('files', f));
  appendServerFiles(input, fd);
  fd.append('target', document.getElementById('imgconv-target').value);
  postForm('/api/imgconvert', fd, 'imgconv-status', this, 'converted image(s)');
});

// --- Image compression ---
wireDrop('imgcomp-drop', 'imgcomp-files', files => {
  document.getElementById('imgcomp-list').textContent = [...files].map(f => f.name).join(', ');
});
document.querySelectorAll('input[name=comp-mode]').forEach(r => r.addEventListener('change', () => {
  const target = document.querySelector('input[name=comp-mode]:checked').value === 'target';
  document.getElementById('comp-target-box').style.display = target ? '' : 'none';
  document.getElementById('comp-quality-box').style.display = target ? 'none' : '';
}));
document.getElementById('comp-quality').addEventListener('input', e => {
  document.getElementById('comp-q-val').textContent = e.target.value;
});

document.getElementById('imgcomp-go').addEventListener('click', async function () {
  const input = document.getElementById('imgcomp-files');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('imgcomp-status', 'error', 'Choose images first'); return; }
  const mode = document.querySelector('input[name=comp-mode]:checked').value;
  const fd = new FormData();
  [...input.files].forEach(f => fd.append('files', f));
  appendServerFiles(input, fd);
  fd.append('mode', mode);
  fd.append('format', document.getElementById('comp-format').value);
  fd.append('target_kb', document.getElementById('comp-kb').value || 200);
  fd.append('quality', document.getElementById('comp-quality').value || 80);

  this.disabled = true;
  document.getElementById('comp-report').innerHTML = '';
  setStatus('imgcomp-status', 'working', '&#9203; compressing...');
  try {
    const j = await uploadForm('/api/imgcompress', fd, 'imgcomp-status');
    this.disabled = false;
    if (j.error) { setStatus('imgcomp-status', 'error', '&#10008; ' + j.error); return; }
    doneLink('imgcomp-status', j.job_id, 'compressed image(s)');
    renderCompReport(j.report);
  } catch (e) {
    this.disabled = false;
    setStatus('imgcomp-status', 'error', '&#10008; ' + e);
  }
});

function renderCompReport(report) {
  if (!report || !report.length) return;
  let html = '<table class="comp-table"><tr><th>File</th><th>Before</th><th>After</th><th>Saved</th><th>Settings</th></tr>';
  report.forEach(r => {
    const pct = r.original_kb > 0 ? Math.round((1 - r.new_kb / r.original_kb) * 100) : 0;
    const settings = `q${r.quality}` + (r.scale < 100 ? `, ${r.scale}% size` : '');
    html += `<tr><td class="fname" title="${r.name}">${r.name}</td>`
      + `<td>${r.original_kb} KB</td>`
      + `<td class="${r.hit_target ? '' : 'miss'}">${r.new_kb} KB${r.hit_target ? '' : ' &#9888;'}</td>`
      + `<td class="saved">${pct > 0 ? pct + '%' : '—'}</td>`
      + `<td>${settings}</td></tr>`;
  });
  html += '</table>';
  if (report.some(r => !r.hit_target)) {
    html += '<p class="hint">&#9888; Could not reach the target size even at lowest quality — this is the smallest achievable. Try a smaller target, or WebP for better compression.</p>';
  }
  document.getElementById('comp-report').innerHTML = html;
  revealChildren(document.getElementById('comp-report'), 'tr');
}

// --- Images to PDF ---
let pdfAlign = 'center';
document.querySelectorAll('#pdf-align button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#pdf-align button').forEach(x => x.classList.remove('sel'));
  b.classList.add('sel');
  pdfAlign = b.dataset.v;
}));

// ----- free layout editor state -----
const PAGE_PTS = { a4: [595, 842], letter: [612, 792], legal: [612, 1008], a5: [420, 595] };
const ED_W = 440;
const ed = { items: [], pages: 1, cur: 0, sel: null, urls: [] };
const canvas = document.getElementById('ed-canvas');

function pdfMode() { return document.querySelector('input[name=pdf-mode]:checked').value; }

function updateModeUI() {
  const custom = pdfMode() === 'custom';
  document.getElementById('pdf-auto-opts').style.display = custom ? 'none' : '';
  document.getElementById('pdf-editor').style.display = custom ? '' : 'none';
  document.getElementById('pdf-fit-opt').disabled = custom;
  if (custom && document.getElementById('pdf-pagesize').value === 'fit')
    document.getElementById('pdf-pagesize').value = 'a4';
  if (custom) initEditor();
}
document.querySelectorAll('input[name=pdf-mode]').forEach(r => r.addEventListener('change', updateModeUI));
document.getElementById('pdf-pagesize').addEventListener('change', () => { if (pdfMode() === 'custom') resizeCanvas(); });
document.getElementById('pdf-orient').addEventListener('change', () => { if (pdfMode() === 'custom') resizeCanvas(); });

function pageAspect() {
  let [w, h] = PAGE_PTS[document.getElementById('pdf-pagesize').value] || PAGE_PTS.a4;
  if (document.getElementById('pdf-orient').value === 'landscape') [w, h] = [h, w];
  return h / w;
}
function resizeCanvas() {
  canvas.style.width = ED_W + 'px';
  canvas.style.height = Math.round(ED_W * pageAspect()) + 'px';
  renderEditor();
}

function initEditor() {
  const files = pdfFiles;
  ed.urls.forEach(u => URL.revokeObjectURL(u));
  ed.urls = files.map(f => URL.createObjectURL(f));
  ed.items = []; ed.pages = 1; ed.cur = 0; ed.sel = null;
  let loaded = 0;
  files.forEach((f, i) => {
    const img = new Image();
    img.onload = () => {
      const aspect = img.naturalHeight / img.naturalWidth;
      const w = 0.42, h = w * aspect / pageAspect();
      ed.items.push({ file: i, page: 0, x: 0.05 + (i % 4) * 0.12, y: 0.05 + (i % 4) * 0.1, w, h, aspect });
      if (++loaded === files.length) renderEditor();
    };
    img.src = ed.urls[i];
  });
  resizeCanvas();
}

function renderEditor() {
  canvas.innerHTML = '';
  const cw = canvas.clientWidth, ch = canvas.clientHeight;
  ed.items.forEach((it, idx) => {
    if (it.page !== ed.cur) return;
    const d = document.createElement('div');
    d.className = 'ed-item' + (ed.sel === idx ? ' sel' : '');
    d.style.cssText = `left:${it.x * cw}px; top:${it.y * ch}px; width:${it.w * cw}px; height:${it.h * ch}px; background-image:url('${ed.urls[it.file]}')`;
    const rsz = document.createElement('div');
    rsz.className = 'rsz';
    d.appendChild(rsz);
    d.addEventListener('mousedown', e => startDrag(e, idx, e.target === rsz));
    canvas.appendChild(d);
  });
  document.getElementById('ed-pageinfo').textContent = `Page ${ed.cur + 1} / ${ed.pages}`;
  const has = ed.sel !== null;
  document.getElementById('ed-movenext').disabled = !has;
  document.getElementById('ed-remove').disabled = !has;
}

function startDrag(e, idx, resizing) {
  e.preventDefault();
  ed.sel = idx;
  renderEditor();
  const it = ed.items[idx];
  const cw = canvas.clientWidth, ch = canvas.clientHeight;
  const sx = e.clientX, sy = e.clientY;
  const ox = it.x, oy = it.y, ow = it.w, oh = it.h;
  function move(ev) {
    const dx = (ev.clientX - sx) / cw, dy = (ev.clientY - sy) / ch;
    if (resizing) {
      it.w = Math.max(0.05, ow + dx);
      it.h = it.w * it.aspect / pageAspect();   // keep image proportions
    } else {
      it.x = Math.min(Math.max(ox + dx, -it.w + 0.02), 0.98);
      it.y = Math.min(Math.max(oy + dy, -it.h + 0.02), 0.98);
    }
    renderEditor();
  }
  function up() { window.removeEventListener('mousemove', move); window.removeEventListener('mouseup', up); }
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', up);
}

document.getElementById('ed-prev').addEventListener('click', () => { if (ed.cur > 0) { ed.cur--; ed.sel = null; renderEditor(); } });
document.getElementById('ed-next').addEventListener('click', () => { if (ed.cur < ed.pages - 1) { ed.cur++; ed.sel = null; renderEditor(); } });
document.getElementById('ed-addpage').addEventListener('click', () => { ed.pages++; ed.cur = ed.pages - 1; ed.sel = null; renderEditor(); });
document.getElementById('ed-movenext').addEventListener('click', () => {
  if (ed.sel === null) return;
  if (ed.cur === ed.pages - 1) ed.pages++;
  ed.items[ed.sel].page = ed.cur + 1;
  ed.sel = null; renderEditor();
});
document.getElementById('ed-remove').addEventListener('click', () => {
  if (ed.sel === null) return;
  ed.items.splice(ed.sel, 1);
  ed.sel = null; renderEditor();
});
canvas.addEventListener('mousedown', e => { if (e.target === canvas) { ed.sel = null; renderEditor(); } });

document.getElementById('ed-apply-preset').addEventListener('click', () => {
  const pageItems = ed.items.filter(it => it.page === ed.cur);
  if (!pageItems.length) return;
  let preset = document.getElementById('ed-preset').value;
  let rows, cols;
  if (preset === 'auto') {
    cols = Math.ceil(Math.sqrt(pageItems.length));
    rows = Math.ceil(pageItems.length / cols);
  } else {
    [rows, cols] = preset.split('x').map(Number);
  }
  const GAP = 0.02;                      // outer margin + spacing between cells
  const cellW = (1 - GAP * (cols + 1)) / cols;
  const cellH = (1 - GAP * (rows + 1)) / rows;
  pageItems.forEach((it, i) => {
    if (i >= rows * cols) return;        // extras stay where they are
    const r = Math.floor(i / cols), c = i % cols;
    const cellX = GAP + c * (cellW + GAP);
    const cellY = GAP + r * (cellH + GAP);
    // fit image inside its cell, preserving aspect ratio, centered
    const imgAspect = it.aspect / pageAspect();  // height-fraction per width-fraction
    let w = cellW, h = w * imgAspect;
    if (h > cellH) { h = cellH; w = h / imgAspect; }
    it.w = w; it.h = h;
    it.x = cellX + (cellW - w) / 2;
    it.y = cellY + (cellH - h) / 2;
  });
  ed.sel = null;
  renderEditor();
});

// ordered list of chosen images (user can drag to reorder)
let pdfFiles = [];

function renderPdfThumbs() {
  const list = document.getElementById('pdf-list');
  list.innerHTML = '';
  pdfFiles.forEach((f, i) => {
    const t = document.createElement('div');
    t.className = 'thumb';
    t.draggable = true;
    t.dataset.i = i;
    t.innerHTML = `<span class="pg">${i + 1}</span><span class="rm">&times;</span>`
      + `<img src="${f._url}" alt=""><div class="nm">${f.name}</div>`;
    t.querySelector('.rm').addEventListener('click', e => {
      e.stopPropagation();
      URL.revokeObjectURL(pdfFiles[i]._url);
      pdfFiles.splice(i, 1);
      syncPdfFiles();
    });
    t.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', i); t.classList.add('dragging'); });
    t.addEventListener('dragend', () => t.classList.remove('dragging'));
    t.addEventListener('dragover', e => { e.preventDefault(); t.classList.add('drag-over'); });
    t.addEventListener('dragleave', () => t.classList.remove('drag-over'));
    t.addEventListener('drop', e => {
      e.preventDefault(); t.classList.remove('drag-over');
      const from = +e.dataTransfer.getData('text/plain'), to = i;
      if (from === to) return;
      const [moved] = pdfFiles.splice(from, 1);
      pdfFiles.splice(to, 0, moved);
      syncPdfFiles();
    });
    list.appendChild(t);
  });
  revealChildren(list, '.thumb');
}

// keep the editor in sync with the ordered file list
function syncPdfFiles() {
  renderPdfThumbs();
  document.getElementById('pdf-reverse').style.display = pdfFiles.length > 1 ? '' : 'none';
  if (pdfMode() === 'custom') initEditor();
}

document.getElementById('pdf-reverse').addEventListener('click', () => {
  pdfFiles.reverse();
  syncPdfFiles();
});

wireDrop('pdf-drop', 'pdf-files', files => {
  pdfFiles.forEach(f => URL.revokeObjectURL(f._url));
  pdfFiles = [...files];
  pdfFiles.forEach(f => { f._url = URL.createObjectURL(f); });
  syncPdfFiles();
});

document.getElementById('pdf-go').addEventListener('click', function () {
  if (!pdfFiles.length) { setStatus('pdf-status', 'error', 'Choose images first'); return; }
  const fd = new FormData();
  pdfFiles.forEach(f => fd.append('files', f));
  fd.append('page_size', document.getElementById('pdf-pagesize').value);
  fd.append('orientation', document.getElementById('pdf-orient').value);
  if (pdfMode() === 'custom') {
    if (!ed.items.length) { setStatus('pdf-status', 'error', 'No images placed in the editor'); return; }
    fd.append('placements', JSON.stringify(ed.items.map(it => ({ file: it.file, page: it.page, x: it.x, y: it.y, w: it.w, h: it.h }))));
    postForm('/api/img2pdf_custom', fd, 'pdf-status', this, 'PDF');
  } else {
    fd.append('margin', document.getElementById('pdf-margin').value || 0);
    fd.append('scale', document.getElementById('pdf-scale').value || 100);
    fd.append('align', pdfAlign);
    postForm('/api/img2pdf', fd, 'pdf-status', this, 'PDF');
  }
});

// --- Merge PDFs ---
let mergeFiles = [];

function renderMergeRows() {
  const list = document.getElementById('merge-list');
  list.innerHTML = '';
  mergeFiles.forEach((f, i) => {
    const row = document.createElement('div');
    row.className = 'filerow';
    row.draggable = true;
    row.innerHTML = `<span class="idx">${i + 1}</span><span class="fn">${f.name}</span><span class="rm">&times;</span>`;
    row.querySelector('.rm').addEventListener('click', e => {
      e.stopPropagation();
      mergeFiles.splice(i, 1);
      syncMergeFiles();
    });
    row.addEventListener('dragstart', e => { e.dataTransfer.setData('text/plain', i); row.classList.add('dragging'); });
    row.addEventListener('dragend', () => row.classList.remove('dragging'));
    row.addEventListener('dragover', e => { e.preventDefault(); row.classList.add('drag-over'); });
    row.addEventListener('dragleave', () => row.classList.remove('drag-over'));
    row.addEventListener('drop', e => {
      e.preventDefault(); row.classList.remove('drag-over');
      const from = +e.dataTransfer.getData('text/plain'), to = i;
      if (from === to) return;
      const [moved] = mergeFiles.splice(from, 1);
      mergeFiles.splice(to, 0, moved);
      syncMergeFiles();
    });
    list.appendChild(row);
  });
  revealChildren(list, '.filerow');
}

function syncMergeFiles() {
  renderMergeRows();
  document.getElementById('merge-reverse').style.display = mergeFiles.length > 1 ? '' : 'none';
}

document.getElementById('merge-reverse').addEventListener('click', () => {
  mergeFiles.reverse();
  syncMergeFiles();
});

wireDrop('merge-drop', 'merge-files', files => {
  mergeFiles = [...files];
  syncMergeFiles();
});
document.getElementById('merge-go').addEventListener('click', function () {
  if (mergeFiles.length < 2) { setStatus('merge-status', 'error', 'Choose at least two PDFs'); return; }
  const fd = new FormData();
  mergeFiles.forEach(f => fd.append('files', f));
  postForm('/api/pdf/merge', fd, 'merge-status', this, 'merged PDF');
});

// --- PDF to images ---
wireDrop('p2i-drop', 'p2i-file', files => {
  if (files.length) document.getElementById('p2i-drop-text').textContent = files[0].name;
});
document.getElementById('p2i-go').addEventListener('click', function () {
  const input = document.getElementById('p2i-file');
  if (!input.files.length && !(input.serverFiles || []).length) { setStatus('p2i-status', 'error', 'Choose a PDF first'); return; }
  const fd = new FormData();
  if (input.files.length) fd.append('file', input.files[0]);
  appendServerFiles(input, fd);
  fd.append('format', document.getElementById('p2i-format').value);
  fd.append('dpi', document.getElementById('p2i-dpi').value);
  postForm('/api/pdf2img', fd, 'p2i-status', this, 'images');
});
