'use strict';
/* "Ask the library" — chat with the local model, grounded in the library. Conversations stay on this device only. */
const Chat = (() => {
  const STARTERS = [
    ['water', 'The tap water is off. How do I make water safe to drink?'],
    ['hurt', 'Someone has a deep cut that won’t stop bleeding. What do I do?'],
    ['power', 'How do I run a generator safely during a power outage?'],
    ['food', 'The fridge has been off for 6 hours. What food is still safe?'],
    ['sanitation', 'The toilets don’t work. How do we make an emergency toilet?'],
    ['comms', 'Phones are down. How can our household stay in contact?'],
    ['grow', 'What vegetables can I plant this month?'],
    ['repair', 'How do I change the oil on a Honda EU2200i generator?'],
  ];
  // The Chat page's first screen: one card per urgent guide, each asking its guide's starter question, then a few learning guides.
  // The ink drawings are the website's own (website-next/img/ink/), copied flat into web/ because builds ship web/ without folders.
  const HOME = [['water', 'water'], ['hurt', 'aid'], ['food', 'food'], ['power', 'power'], ['comms', 'radio'], ['grow', 'garden']];
  const LEARN = [['grow', 'garden'], ['health', 'carer'], ['energy', 'lantern']];
  const ABOUT_STARTERS = { doc: ['Explain this page in simple words', 'What are the warnings on this page?', 'Is there a video that shows this?'],
    video: ['Summarise what this video shows', 'What safety points does this video make?', 'Find a manual page that covers the same thing'],
    topic: ['What should I do first?', 'What are the most important warnings?', 'Which video should I watch first?'] };
  const STATES = { starting: 'Starting the local AI…', thinking: 'Thinking…', searching: 'Searching the library…', reading: 'Reading the pages it found…', writing: 'Writing the answer…' };
  // Where an answer is: search, then read, then write (none yet while the local AI starts). A later round can search again, so a step can light up a second time.
  const STAGES = ['Search', 'Read', 'Write'], STAGE_AT = { starting: -1, thinking: 0, searching: 0, reading: 1, writing: 2 };
  function progress(m) {
    const at = STAGE_AT[m.state] ?? 0, pct = m.state === 'starting' && m.pct ? Math.max(0, Math.min(100, +m.pct || 0)) : null;
    const stages = STAGES.map((t, i) => `<li class="${i < at ? 'done' : i === at ? 'now' : ''}">${t}</li>`).join('');
    return `<ol class="wk-stages" aria-hidden="true">${stages}</ol><span class="wk-bar${pct === null ? ' run' : ''}" aria-hidden="true"><span${pct === null ? '' : ` style="width:${pct}%"`}></span></span>`;
  }
  let chats = load(), live = {};   // live[chatId] = {abort, state, steps}
  let status = null;

  function load() { try { return JSON.parse(localStorage.getItem('sg.chats')) || [] } catch { return [] } }
  const keep = () => { try { return localStorage.getItem('sg.chatKeep') !== 'no' } catch { return true } };
  function persist() { if (!keep()) return; try { localStorage.setItem('sg.chats', JSON.stringify(chats.slice(0, 60))) } catch { } }
  const byId = id => chats.find(c => c.id === id);
  const newId = () => Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  const count = () => chats.length;

  async function getStatus() {
    try { status = await (await fetch('/api/assistant/status')).json() } catch { status = { online: false, error: 'server' } }
    return status;
  }

  /* ---------------------------------------------------------------- page -- */
  let root = null, cur = null, aboutCtx = null;
  async function page(main, parts, q) {
    root = main;
    const id = parts[0];
    cur = id ? byId(id) : null;
    if (id && !cur) { swap('#/ask'); return }
    const about = q.get('about');
    aboutCtx = !cur && about ? await describe(about) : (cur && cur.about ? cur.aboutInfo : null);
    document.title = (cur ? cur.title : 'Chat') + ' · Lastbook';
    $('#tbTitle').textContent = cur ? cur.title : 'Chat';
    main.innerHTML = `<div class="chat">
      <aside class="chat-side" id="chatSide" aria-label="Conversations">
        <div class="cs-head"><b>Chats</b><button class="iconbtn" id="closeSide" aria-label="Close chat list">${ico('back')}</button></div>
        <a class="btn plain" href="#/ask" id="newChat2">${ico('ask')}New chat</a>
        <ul class="cs-list" id="csList"></ul>
        <div class="cs-privacy">
          <label><input type="checkbox" id="keepChats" ${keep() ? 'checked' : ''}> Keep chat history on this device</label>
          <p class="small muted">Chats stay on this device. Nothing is sent anywhere except this computer’s own assistant.</p>
          <button class="btn plain" id="clearChats">Delete all chats</button>
        </div>
      </aside>
      <section class="chat-main">
        <div class="chat-top">
          <button class="btn plain chat-histbtn" id="openSide" aria-label="Show chats">${ico("clock")}<span>Chats</span></button>
          <div class="chat-status" id="chatStatus" aria-live="polite"></div>
          <a class="btn plain" href="#/ask" id="newChat" aria-label="New chat">${ico('ask')}<span>New</span></a>
        </div>
        ${q.get('from') && !cur ? `<div style="margin:2px 0 8px">${backButton(q.get('from'))}</div>` : ''}
        <div class="ai-setup" id="aiSetup" hidden></div>
        <div class="chat-log" id="chatLog" role="log" aria-live="polite" aria-label="Conversation"></div>
        <form class="composer" id="composer">
          <div class="ctxchip" id="ctxChip" hidden></div>
          <div class="comp-row">
            <label class="sr" for="chatIn">Your question</label>
            <textarea id="chatIn" rows="1" placeholder="Ask a question…" enterkeyhint="send"></textarea>
            <button class="sendbtn" id="sendBtn" aria-label="Send">${ico("up")}</button>
          </div>
          <p class="comp-note">Answers come from the library on this computer and can be wrong. Check the linked pages; get trained help for anything serious.</p>
        </form>
        <div class="ask-more" id="askMore" hidden></div>
      </section></div>`;
    renderSide(); renderCtx(); renderLog();
    const ta = $('#chatIn');
    ta.addEventListener('input', autosize);
    ta.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); $('#composer').requestSubmit() }
      if (e.key === 'Escape') stop();
    });
    $('#composer').addEventListener('submit', e => { e.preventDefault(); if (busy()) return stop(); send(ta.value) });
    $('#openSide').onclick = () => $('#chatSide').classList.add('open');
    $('#closeSide').onclick = () => $('#chatSide').classList.remove('open');
    $('#keepChats').onchange = e => {
      try { localStorage.setItem('sg.chatKeep', e.target.checked ? 'yes' : 'no') } catch { }
      if (!e.target.checked) { try { localStorage.removeItem('sg.chats') } catch { } toast('Chats will be forgotten when you close this page') } else { persist(); toast('Chats will be kept on this device') }
    };
    $('#clearChats').onclick = () => { if (!confirm('Delete all chats on this device?')) return; Object.values(live).forEach(l => l.abort && l.abort()); chats = []; try { localStorage.removeItem('sg.chats') } catch { } location.hash = '#/ask' };
    leaving = () => { document.removeEventListener('keydown', escStop) };
    document.addEventListener('keydown', escStop);
    statusLine();
    const initial = q.get('q');
    if (initial && !cur) { history.replaceState(null, '', '#/ask'); send(initial) }
    else if (matchMedia('(hover:hover)').matches) setTimeout(() => ta.focus({ preventScroll: true }), 60);
    const y = scrolls[here()];
    if (y != null) requestAnimationFrame(() => scrollTo(0, y)); else if (cur && cur.messages.length) scrollEnd(true); else scrollTo(0, 0);   // a new chat opens at its top
  }
  const escStop = e => { if (e.key === 'Escape' && busy()) stop() };
  function autosize() { const ta = $('#chatIn'); if (!ta) return; ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 180) + 'px' }
  const busy = () => cur && live[cur.id] && live[cur.id].running;
  // The optional local AI (desktop app): [ready?, the line to show]. The server version has no s.local.
  function localLine(l) {
    const gb = n => (n / 1e9).toFixed(0) + ' GB';
    return ({
      ready: [true, 'Local AI ready'],
      stopped: [true, 'Local AI ready · it starts when you ask'],
      loading: [true, `Starting the local AI${l.pct ? ' ' + l.pct + '%' : '…'}`],
      checking: [false, `Checking the local AI file${l.pct != null ? ' ' + l.pct + '%' : '…'}`],
      measuring: [false, 'Checking this computer’s memory…'],
      absent: [false, 'No local AI added'],
      wrong: [false, l.message || 'The local AI file is not the right one.'],
      too_big: [false, l.message || `The local AI needs about ${gb(l.needs_bytes || 0)} of memory.`],
      off: [false, 'Local AI is off'],
      error: [false, l.message || 'The local AI could not start.'],
    })[l.state] || [false, l.message || 'Local AI unavailable'];
  }
  let polling = null;
  async function statusLine() {
    const s = await getStatus(), el = $('#chatStatus'); if (!el) return;
    const ix = s.index || {}, l = s.local;
    const still = ix.state === 'indexing' || ix.state === 'indexing (other process)' ? ` · <span class="muted">still indexing (${(ix.chunks || 0).toLocaleString()} passages so far)</span>` : '';
    if (l && (/ready|stopped|loading/.test(l.state) || !s.online)) {
      const [ok, line] = localLine(l);
      el.innerHTML = `<span class="${ok ? 'dotok' : 'dotoff'}"></span><span>${esc(line)}${ok ? still : '<span class="long"> — search and reading still work</span>'}</span>`;
    } else {
      el.innerHTML = s.online ? `<span class="dotok"></span>Local assistant ready${still}`
        : `<span class="dotoff"></span><span>Assistant offline<span class="long"> — search and browsing still work</span></span>`;
    }
    const sb = $('#sendBtn'); if (sb) sb.title = s.online ? 'Send' : 'The assistant is offline; you will get matching pages instead';
    setupCard(s);
    clearTimeout(polling);
    if (l && /checking|measuring|loading/.test(l.state) && $('#chatStatus')) polling = setTimeout(statusLine, 1000);
  }
  // How to add the local AI, on a new chat when it is missing, wrong or off. The file comes from the website, outside the app.
  function setupCard(s) {
    const box = $('#aiSetup'), l = s.local; if (!box) return;
    const show = !!l && !s.online && /absent|wrong|off|error/.test(l.state) && !(cur && cur.messages.length);
    box.hidden = !show;
    if (!show) return;
    box.innerHTML = l.state === 'off'
      ? `<p>The local AI is turned off.</p><div class="row"><button class="btn" data-ai="on">Turn on</button></div>`
      : l.state === 'error' ? `<p>${esc(l.message || 'The local AI could not start.')}</p><div class="row"><button class="btn" data-ai="rescan">Look again</button></div>`
      : `<p><b>Add the local AI to get written answers.</b> It is about 17 GB, and runs on this computer only.</p>
         <div class="row"><a class="btn" href="/ui/setup.html#manage">Add the local AI</a><button class="btn plain" data-ai="rescan">Look again</button></div>
         <p class="small">Already downloaded <code>${esc(l.file)}</code>? Move it into the <b>models</b> folder inside your <b>LastbookData</b> folder, then Look again. Or open <b>Add the local AI</b> and pick the file you already have.</p>`;
    $$('[data-ai]', box).forEach(b => b.onclick = async () => {
      b.disabled = true;
      try { await fetch('/api/assistant/local', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: b.dataset.ai }) }) } catch { }
      setTimeout(statusLine, 300);
    });
  }
  async function describe(about) {
    const [kind, id, n] = about.split(':');
    try {
      if (kind === 'topic') { const t = (await topics()).find(x => x.id === id); return { about, label: t ? (SHORT[id] || t.title) : id, sub: 'Guide', kind } }
      const d = await api('/api/doc/' + encodeURIComponent(id));
      return { about, kind, label: d.title, sub: kind === 'video' ? (n ? 'Video at ' + clock(+n) : 'Video') : 'Page ' + (n || 1), href: kind === 'video' ? `#/watch/${encodeURIComponent(id)}${n ? '?t=' + n : ''}` : `#/read/${encodeURIComponent(id)}?p=${n || 1}` };
    } catch { return null }
  }
  function renderCtx() {
    const el = $('#ctxChip'); if (!el) return;
    const a = aboutCtx;
    if (!a) { el.hidden = true; return }
    el.hidden = false;
    el.innerHTML = `${ico(a.kind === 'video' ? 'video' : a.kind === 'topic' ? 'book' : 'page')}<span>Asking about: <b>${esc(a.label)}</b> <span class="muted">${esc(a.sub || '')}</span></span>${cur && cur.messages.length ? '' : '<button type="button" aria-label="Don’t use this as context" id="ctxX">×</button>'}`;
    const x = $('#ctxX'); if (x) x.onclick = () => { aboutCtx = null; renderCtx(); renderLog() };
  }
  function renderSide() {
    const el = $('#csList'); if (!el) return;
    el.innerHTML = chats.length ? chats.map(c => `<li><a href="#/ask/${c.id}" ${cur && cur.id === c.id ? 'aria-current="page"' : ''}><b>${esc(c.title)}</b><small>${new Date(c.updated).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} · ${c.messages.filter(m => m.role === 'user').length} question${c.messages.filter(m => m.role === 'user').length > 1 ? 's' : ''}</small></a><button class="iconbtn" data-del="${c.id}" aria-label="Delete this chat">×</button></li>`).join('')
      : '<li class="muted small" style="padding:8px 4px">No chats yet.</li>';
    const clr = $('#clearChats'); if (clr) clr.hidden = !chats.length;   // nothing to delete
    $$('[data-del]', el).forEach(b => b.onclick = () => { const id = b.dataset.del; live[id]?.abort?.(); chats = chats.filter(c => c.id !== id); persist(); if (cur && cur.id === id) location.hash = '#/ask'; else renderSide() });
    // the list fades at an edge with more chats past it, so a row cut off there reads as more to scroll to
    const edge = () => { const more = el.scrollHeight - el.clientHeight; el.classList.toggle('more-b', more > 1 && el.scrollTop < more - 1); el.classList.toggle('more-t', more > 1 && el.scrollTop > 1) };
    el.onscroll = edge; edge();
    if (!el.lbEdge) { el.lbEdge = new ResizeObserver(edge); el.lbEdge.observe(el) }
  }

  /* --------------------------------------------------------------- log --- */
  function renderLog() {
    const log = $('#chatLog'); if (!log) return;
    const empty = !cur || !cur.messages.length, home = empty && !aboutCtx, more = $('#askMore');
    log.closest('.chat-main').classList.toggle('home', home);
    if (more) { more.hidden = !home; more.innerHTML = home ? homeMore() : '' }
    if (home) {
      log.innerHTML = `<div class="ask-hero">
        <div><h1>What do you need to know?</h1>
        <p class="lead">Answers from the guides, manuals and videos on this computer, with the page or video moment for each point.</p></div>
        <img class="ink-art" src="/ui/ink-pair.svg" alt="" width="240" height="120"></div>`;
      $$('[data-start]', more).forEach(b => b.onclick = () => send(b.dataset.start));
      return;
    }
    if (empty) {
      const st = ABOUT_STARTERS[aboutCtx.kind] || [];
      log.innerHTML = `<div class="chat-empty">
        <div class="ce-mark">${ico('ask')}</div>
        <h1>Chat</h1>
        <p class="lead">Answers are built only from the manuals, guides and video transcripts on this computer — with the exact page or video moment for each point.</p>
        <div class="starters">${st.map(t => ['ask', t]).map(([i, t]) => `<button class="starter" ${tcStyle(i)} data-start="${esc(t)}"><span class="ico">${ico(i)}</span><span>${esc(t)}</span></button>`).join('')}</div>
        <p class="small muted" style="margin-top:18px">It reads text only: it can’t see inside videos without captions, scanned pages without text, or raw map files. It tells you when the library has nothing on a question.</p></div>`;
      $$('[data-start]', log).forEach(b => b.onclick = () => send(b.dataset.start));
      fitEmpty();
      return;
    }
    log.innerHTML = cur.messages.map((m, i) => m.role === 'user' ? `<div class="msg user"><div class="bubble">${esc(m.content)}</div></div>` : `<div class="msg bot" id="m${i}">${botHtml(m, i)}</div>`).join('');
    wire(log);
  }
  function homeMore() {
    const q = Object.fromEntries(STARTERS), title = id => esc(SHORT[id] || id);
    return `<p class="ask-safe"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3 4.5 6v5.5c0 4.4 3.2 8.2 7.5 9.5 4.3-1.3 7.5-5.1 7.5-9.5V6z"/><path d="m8.8 12 2.2 2.2 4.2-4.6"/></svg>Runs on this computer. Nothing you ask leaves it.</p>
      <h2 class="ask-h">Start with what matters</h2>
      <div class="ask-cards">${HOME.map(([id, art]) => `<button class="ask-card" ${tcStyle(id)} data-start="${esc(q[id])}">
        <span class="ink-tile"><img src="/ui/ink-${art}.svg" alt="" width="44" height="44"></span><span class="ac-t"><b>${title(id)}</b><span>${esc(q[id])}</span></span>${ico('right', 'ac-go')}</button>`).join('')}</div>
      <div class="ask-learn-head"><h2 class="ask-h">Keep learning</h2><a href="#/library">All skills</a></div>
      <div class="ask-learn">${LEARN.map(([id, art]) => `<a href="#/t/${id}" ${tcStyle(id)}><span class="ink-tile"><img src="/ui/ink-${art}.svg" alt="" width="44" height="44"></span><span><b>${title(id)}</b><small>Step-by-step guide</small></span></a>`).join('')}</div>`;
  }
  // The page itself never scrolls while a chat is empty: in a window too short for every suggestion (or with lines wrapped narrow beside
  // the chats column), the last ones go, a row at a time down to one row, then the note under them, the introduction and the mark.
  function fitEmpty() {
    const e = $('.chat-empty'); if (!e) return;
    const all = $$('.starter', e), extra = ['p.small', '.lead', '.ce-mark'].map(s => $(s, e)).filter(Boolean);
    [...all, ...extra].forEach(x => x.style.removeProperty('display'));
    const over = () => document.scrollingElement.scrollHeight > innerHeight;
    if (!over()) return;
    const shown = all.filter(x => x.offsetWidth);
    for (let k = shown.length; k > 2 && over(); k -= 2) shown.slice(k - 2, k).forEach(x => x.style.display = 'none');
    for (const x of extra) if (over()) x.style.display = 'none';
  }
  addEventListener('resize', () => { if ($('.chat-empty')) fitEmpty() });
  function updateBot(i) {
    const el = $('#m' + i); if (!el || !cur) return renderLog();
    el.innerHTML = botHtml(cur.messages[i], i); wire(el);
  }
  function botHtml(m, i) {
    const l = live[cur.id], running = l && l.running && i === cur.messages.length - 1;
    const steps = (m.steps || []).map(s => `<li>${stepText(s)}</li>`).join('');
    const found = m.sources || [];
    let h = '';
    if (running) h += `<div class="working"><b>${esc(STATES[m.state] || 'Working…')}${m.state === 'starting' && m.pct ? ` ${m.pct}%` : ''}</b>${progress(m)}${steps ? `<ul class="steps">${steps}</ul>` : ''}</div>`;
    if (m.error) h += `<div class="ans-error">${ico('warn')}<div><b>${esc(m.error)}</b>${m.offline ? '<p>You can keep browsing and searching. Here are the pages that best match your question:</p>' : ''}</div></div>`;
    if (m.content) h += `<div class="answer">${md(m.content, m)}</div>`;
    if (running && found.length) h += `<div class="prefound"><p class="small muted">Pages found so far — you can open them while the answer is written:</p>${srcCards(found.slice(0, 6), true)}</div>`;
    if (m.fallback) h += srcCards(m.fallback, true);
    if (!running && m.content) {
      const cited = m.cited || [];
      if ((m.invalid || []).length) h += `<p class="small" style="color:var(--stop)">The model referred to sources that don’t exist (${m.invalid.join(', ')}); those markers were removed.</p>`;
      h += `<div class="legend small"><span><span class="lg-c">1</span> linked to a page or video</span><span><span class="lg-u">dotted</span> the model’s own wording, not linked to a source</span></div>`;
      if (cited.length) h += `<h3 class="srch">Sources (${cited.length})</h3>${srcCards(cited, false)}`;
      // A route's turns, word for word from the engine: the model is not asked to retype them.
      for (const s of cited) if (Array.isArray(s.steps) && s.steps.length) h += `<details class="also" open><summary>Turn-by-turn steps (${s.steps.length}), as worked out from the road data</summary><ol class="dirsteps">${s.steps.map(t => `<li>${esc(t)}</li>`).join('')}</ol></details>`;
      else h += `<p class="notice small">This answer has no linked sources. Treat it as unverified.</p>`;
      const other = (m.consulted || []).filter(s => s.kind !== 'place');
      if (other.length) h += `<details class="also"><summary>Also looked at (${other.length}, not used)</summary>${srcCards(other.slice(0, 10), true)}</details>`;
      if ((m.steps || []).length) h += `<details class="also"><summary>How it searched (${m.steps.length} ${m.steps.length === 1 ? 'step' : 'steps'}${m.latency ? `, ${m.latency.total_s}s` : ''})</summary><ul class="steps">${steps}</ul></details>`;
      h += `<div class="ans-actions"><button class="btn plain" data-act="copy" data-i="${i}">Copy</button><button class="btn plain" data-act="save" data-i="${i}">${ico('save')}Save answer</button><button class="btn plain" data-act="export">Export chat</button>${i === cur.messages.length - 1 ? `<button class="btn plain" data-act="retry">Ask again</button>` : ''}</div>`;
    }
    return h;
  }
  function stepText(s) {
    // s.args is the model's own tool call, as it sent it: use only plain text and numbers from it, never convert anything else.
    const plain = v => typeof v === 'string' || typeof v === 'number' ? v : '';
    const raw = s.args && typeof s.args === 'object' ? s.args : {}, r = s.summary || {};
    const a = { query: plain(raw.query), kind: plain(raw.kind), doc_id: plain(raw.doc_id), page: plain(raw.page), pages: raw.pages, article: plain(raw.article) };
    if (s.name === 'search_library') return `Searched “${esc(a.query || '')}”${a.kind && a.kind !== 'any' ? ' (' + esc(a.kind) + 's)' : ''} — ${r.count ?? '…'} passage${r.count === 1 ? '' : 's'}`;
    if (s.name === 'read_pages') {
      const pages = (Array.isArray(a.pages) ? a.pages : [a.pages]).filter(p => typeof p === 'number' || typeof p === 'string');
      return r.error ? 'Tried to read an unknown document' : `Read ${esc(r.title || a.doc_id)}, page${pages.length > 1 ? 's' : ''} ${esc(pages.join(', '))}`;
    }
    if (s.name === 'view_page') return r.error ? 'Couldn’t view that page' : `Looked at the picture of ${esc(r.title || '')}, page ${esc(a.page)}`;
    if (s.name === 'get_timed_transcript') return r.error ? `No transcript for ${esc(r.title || 'that video')}` : `Read the transcript of ${esc(r.title || '')}`;
    if (s.name === 'lookup_place') return `Looked up “${esc(a.query || '')}” on the offline map`;
    if (s.name === 'get_directions') return r.error ? `Found no road route from “${esc(plain(raw.from))}” to “${esc(plain(raw.to))}”` : `Worked out the road route from ${esc(r.from || '')} to ${esc(r.to || '')} — ${esc(r.miles ?? '…')} miles`;
    if (s.name === 'search_encyclopedia') return `Searched the encyclopedias for “${esc(a.query || '')}” — ${r.count ?? '…'} article${r.count === 1 ? '' : 's'}`;
    if (s.name === 'read_encyclopedia') return r.error ? 'Tried to read an unknown encyclopedia article' : `Read the encyclopedia article “${esc(r.title || a.article)}”`;
    if (s.name === 'found') return `Matched ${s.count} passage${s.count === 1 ? '' : 's'} for your question`;
    return esc(s.name);
  }
  function srcHref(s) {
    if (s.kind === 'place' && s.route) return `#/map?from=${encodeURIComponent(s.route.from)}&to=${encodeURIComponent(s.route.to)}`;   // a route: the map draws it again
    if (s.kind === 'place') return `#/map?lat=${s.lat}&lon=${s.lon}&z=16&pin=${encodeURIComponent(s.title || s.name || 'Place from the answer')}`;
    if (s.kind === 'video') return `#/watch/${encodeURIComponent(s.doc_id)}?${s.t != null ? 't=' + Math.floor(s.t) + '&' : ''}from=${encodeURIComponent(here())}`;
    if (s.kind === 'zim' || s.zim) {
      // s.path is already percent-encoded (encyclopedias.ui_path), as on the Encyclopedias page. A row without one
      // gets each part of its key encoded here: article names can hold ?, % and ".
      const path = typeof s.path === 'string' && s.path ? s.path.replace(/^\/?zim\//, '')
        : String(s.zim || s.doc_id || '').replace(/^\/?zim\//, '').split('/').map(encodeURIComponent).join('/');
      return `#/wiki/${path}?from=${encodeURIComponent(here())}`;
    }
    return `#/read/${encodeURIComponent(s.doc_id)}?${s.page ? 'p=' + s.page + '&' : ''}from=${encodeURIComponent(here())}`;
  }
  function srcCards(list, compact) {
    return `<div class="srccards ${compact ? 'compact' : ''}">${list.map(s => {
      let pv;
      if (s.kind === 'video') pv = `<span class="pv v">${s.thumb ? `<img loading="lazy" alt="" src="${fileUrl(s.thumb)}">` : ''}<span class="pg">${s.t != null ? clock(s.t) : 'video'}</span></span>`;
      else if (s.kind === 'pdf' && s.page) pv = `<span class="pv"><img loading="lazy" alt="" src="/api/page/${encodeURIComponent(s.doc_id)}/${s.page}.png?w=300" onerror="this.remove()"><span class="pg">p. ${s.page}</span></span>`;
      else pv = `<span class="pv w">${ico(s.kind === 'zim' || s.zim ? 'wiki' : s.kind === 'place' ? 'pin' : 'page')}</span>`;
      const where = s.kind === 'video' ? (s.t != null ? 'at ' + clock(s.t) : '') : s.page ? `page ${s.page}${s.pages ? ' of ' + s.pages : ''}` : '';
      return `<a class="srccard" href="${esc(srcHref(s))}" id="src-${esc(s.key || '')}">${pv}<span class="sc-body">${s.key ? `<span class="sc-key">${esc(s.key.replace(/^S/, ''))}</span>` : ''}<b>${esc(s.title)}</b>
        <small>${esc(s.publisher || '')}${s.year ? ' · ' + esc(s.year) : ''}${where ? ' · ' + where : ''}${s.trust ? ' · ' + esc(s.trust) : ''}</small>
        ${!compact && s.excerpt ? `<span class="sc-ex">“${esc(s.excerpt.slice(0, 220))}${s.excerpt.length > 220 ? '…' : ''}”</span>` : ''}
        ${s.image ? '<span class="sc-tag">diagram viewed</span>' : ''}</span></a>`;
    }).join('')}</div>`;
  }

  /* markdown-lite with citation chips; sentences without a citation are marked as the model's own wording */
  function md(text, m) {
    const keys = new Set((m.cited || m.sources || []).map(s => s.key));
    const cite = k => keys.has(k) || !m.cited ? `<a class="cite" href="#src-${k}" data-key="${k}" title="Source ${k}">${k.replace(/^S/, '')}</a>` : '';
    const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/(^|\W)\*(?!\s)(.+?)\*(?=\W|$)/g, '$1<em>$2</em>')
      .replace(/\[([SM]\d+)\]/g, (_, k) => cite(k));
    const lines = text.replace(/\r/g, '').split('\n');
    let out = '', list = null, section = 'main', table = null, before = '';
    const close = () => { if (list) { out += `</${list}>`; list = null } };
    // A table the model copied from a source. It counts as cited when a key is in it or in the line just before it, as the instructions ask.
    // Cells split on bare pipes only: "\|" is a pipe inside a cell.
    const cells = r => {
      const s = r.replace(/^\|/, '').replace(/(?<!\\)\|$/, ''), c = [];
      let cur = '';
      for (let i = 0; i < s.length; i++) {
        if (s[i] === '\\' && s[i + 1] === '|') { cur += '|'; i++ } else if (s[i] === '|') { c.push(cur.trim()); cur = '' } else cur += s[i];
      }
      c.push(cur.trim());
      return c;
    };
    const SEP = /^\|?(\s*:?-{2,}:?\s*\|)*\s*:?-{2,}:?\s*\|?$/;
    const flush = () => {
      if (!table) return;
      const head = table.length > 1 && SEP.test(table[1]) ? cells(table[0]) : null, rows = table.slice(head ? 2 : 0).filter(r => !SEP.test(r)).map(cells);
      const cited = section !== 'main' || m.running || /\[[SM]\d+\]/.test(table.join(' ') + ' ' + before);
      const no = cited ? '' : ' title="Not linked to a source"', width = head ? head.length : (rows[0] || []).length;
      // A row with more or fewer cells than the header would put numbers under the wrong heading, so show the lines as written.
      if (rows.some(r => r.length !== width)) out += `<p class="ans-raw${cited ? '' : ' uncited'}"${no}>${table.map(inline).join('<br>')}</p>`;
      else out += `<div class="ans-table${cited ? '' : ' uncited'}"${no}><table>${head ? `<thead><tr>${head.map(c => `<th>${inline(c)}</th>`).join('')}</tr></thead>` : ''}<tbody>${rows.map(r => `<tr>${r.map(c => `<td>${inline(c)}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
      table = null; before = '';  // a key before one table says nothing about the next
    };
    const mark = raw => {  // wrap sentences that carry no citation (main sections only)
      if (section !== 'main' || m.running) return inline(raw);
      return raw.split(/(?<=[.!?])\s+(?=[A-Z(“"])/).map(sn => /\[[SM]\d+\]/.test(sn) || sn.replace(/\*|_/g, '').trim().length < 28 || /:$/.test(sn.trim()) ? inline(sn) : `<span class="uncited" title="Not linked to a source">${inline(sn)}</span>`).join(' ');
    };
    for (let ln of lines) {
      const t = ln.trim();
      if (/^\|.*\|$/.test(t)) { close(); (table = table || []).push(t); continue }
      flush();
      if (!t) { close(); before = ''; continue }
      before = t;
      const head = /^#{1,4}\s+(.*)$/.exec(t) || /^\*\*([^*]+:)\*\*$/.exec(t) || /^\*\*([^*]+)\*\*:?$/.exec(t);
      const hText = head ? head[1] : '';
      const sec = /not found in the library/i.test(t) ? 'missing' : /sources disagree/i.test(t) ? 'disagree' : /own reasoning/i.test(t) ? 'reasoning' : null;
      if (sec || head) {
        close();
        if (out.includes('<div class="sec-')) out += '</div>';
        section = sec || (head ? 'main' : section);
        if (sec) { out += `<div class="sec-${sec}"><h4>${sec === 'missing' ? 'Not found in the library' : sec === 'disagree' ? 'Sources disagree' : 'The model’s own reasoning — not from the library'}</h4>`; const rest = t.replace(/^[#*\s]*(not found in the library|sources disagree|my own reasoning \(not from the library\)|my own reasoning)[:*\s]*/i, ''); if (rest) out += `<p>${inline(rest)}</p>`; continue }
        out += `<h4>${inline(hText.replace(/:$/, ''))}</h4>`; continue;
      }
      const ul = /^[-*•]\s+(.*)$/.exec(t), ol = /^(\d+)[.)]\s+(.*)$/.exec(t);
      if (ul || ol) {
        const want = ul ? 'ul' : 'ol';
        if (list !== want) { close(); out += `<${want}>`; list = want }
        out += `<li>${mark(ul ? ul[1] : ol[2])}</li>`; continue;
      }
      close(); out += `<p>${mark(t)}</p>`;
    }
    flush(); close();
    if (out.includes('<div class="sec-')) out += '</div>';
    return out;
  }

  function wire(el) {
    $$('[data-act]', el).forEach(b => b.onclick = () => {
      const i = +b.dataset.i, m = cur.messages[i];
      if (b.dataset.act === 'copy') { navigator.clipboard?.writeText(plain(m)).then(() => toast('Copied'), () => toast('Couldn’t copy')) }
      if (b.dataset.act === 'save') {
        const q = cur.messages[i - 1]?.content || cur.title;
        toggleSave({ key: `a:${cur.id}:${i}`, title: 'Answer: ' + q.slice(0, 80), href: '#/ask/' + cur.id, sub: `${(m.cited || []).length} sources · ${new Date().toLocaleDateString()}` }, null);
      }
      if (b.dataset.act === 'export') exportChat(cur);
      if (b.dataset.act === 'retry') { const q = cur.messages[cur.messages.length - 2]?.content; cur.messages.splice(-2, 2); send(q) }
    });
    // citation chips scroll to their card instead of changing the route
    $$('a.cite', el).forEach(a => a.onclick = e => { e.preventDefault(); const c = $('#src-' + a.dataset.key, el.closest('.msg') || el); if (c) { c.scrollIntoView({ block: 'center', behavior: 'smooth' }); c.classList.add('flash'); setTimeout(() => c.classList.remove('flash'), 1200) } });
  }
  function plain(m) {
    const map = Object.fromEntries((m.cited || []).map(s => [s.key, s]));
    let t = m.content.replace(/\[([SM]\d+)\]/g, (_, k) => map[k] ? `[${k.replace(/^S/, '')}]` : '');
    if ((m.cited || []).length) t += '\n\nSources:\n' + m.cited.map(s => `[${s.key.replace(/^S/, '')}] ${s.title} — ${s.publisher || ''}${s.year ? ', ' + s.year : ''}${s.page ? ', page ' + s.page : ''}${s.t != null ? ', at ' + clock(s.t) : ''}  ${location.origin}/${srcHref(s).replace(/&?from=[^&]*/, '')}`).join('\n');
    return t;
  }
  function exportChat(c) {
    let md = `# ${c.title}\n\nExported from Lastbook (offline library) on ${new Date().toLocaleString()}.\nAnswers were written by a local AI model from the library's documents. Check the linked pages; get trained help for anything serious.\n`;
    for (const m of c.messages) md += m.role === 'user' ? `\n## Question\n\n${m.content}\n` : `\n## Answer\n\n${m.content ? plain(m) : '(no answer: ' + (m.error || 'stopped') + ')'}\n`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' }));
    a.download = `survival-chat-${new Date().toISOString().slice(0, 10)}.md`;
    document.body.append(a); a.click(); setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 500);
    toast('Saved as a file on this device');
  }
  // Earlier answers' [S#] keys only mean something inside their own turn; give the model readable references instead.
  function history_(c) {
    return c.messages.filter(m => m.content).map(m => {
      if (m.role !== 'assistant') return { role: m.role, content: m.content };
      const map = Object.fromEntries((m.cited || []).map(s => [s.key, s]));
      return { role: 'assistant', content: m.content.replace(/\[([SM]\d+)\]/g, (_, k) => map[k] ? `(${map[k].title}${map[k].page ? ', p. ' + map[k].page : ''}${map[k].t != null ? ', ' + clock(map[k].t) : ''})` : '') };
    });
  }

  /* -------------------------------------------------------------- send --- */
  async function send(text) {
    text = (text || '').trim();
    if (!text) return;
    if (!cur) {
      cur = { id: newId(), title: text.slice(0, 70), created: Date.now(), updated: Date.now(), messages: [], about: aboutCtx ? aboutCtx.about : null, aboutInfo: aboutCtx };
      chats.unshift(cur);
      history.replaceState(null, '', '#/ask/' + cur.id);
    }
    const c = cur;
    c.messages.push({ role: 'user', content: text });
    const m = { role: 'assistant', content: '', state: 'thinking', steps: [], sources: [], running: true };
    c.messages.push(m);
    c.updated = Date.now(); persist();
    const ta = $('#chatIn'); if (ta) { ta.value = ''; autosize() }
    renderSide(); renderCtx(); renderLog(); setBusy(true); scrollEnd(true);
    const idx = c.messages.length - 1, ctl = new AbortController();
    live[c.id] = { running: true, abort: () => ctl.abort() };
    const redraw = throttle(() => { if (cur === c) { updateBot(idx); scrollEnd() } }, 80);
    try {
      const st = await getStatus();
      if (!st.online) throw Object.assign(new Error('offline'), { offline: true, why: st.local && localLine(st.local)[1] });
      const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ messages: history_(c).slice(0, -1).concat([{ role: 'user', content: text }]), about: c.about }), signal: ctl.signal });
      if (!r.ok || !r.body) throw new Error('The server refused the question (' + r.status + ').');
      const rd = r.body.getReader(), dec = new TextDecoder();
      let buf = '';
      for (;;) {
        const { value, done } = await rd.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let k;
        while ((k = buf.indexOf('\n\n')) >= 0) {
          const chunk = buf.slice(0, k); buf = buf.slice(k + 2);
          if (!chunk.startsWith('data:')) continue;
          let ev; try { ev = JSON.parse(chunk.slice(5)) } catch { continue }
          onEvent(m, ev);
          redraw();
        }
      }
      if (m.running && !m.content && !m.error) m.error = 'The answer stopped unexpectedly.';
    } catch (e) {
      if (e.name === 'AbortError') { m.stopped = true; if (!m.content) m.error = 'Stopped.' }
      else if (e.offline || /model_unavailable/.test(e.message)) { m.offline = true; m.error = e.why || 'The local assistant model isn’t running right now.'; await fallback(m, text) }
      else m.error = e.message || 'Something went wrong.';
    }
    m.running = false; delete m.state;
    if (m.stopped && m.content) m.content += '\n\n(Stopped.)';
    live[c.id].running = false;
    c.updated = Date.now(); persist();
    if (cur === c) { updateBot(idx); setBusy(false); scrollEnd(); const t = $('#chatIn'); if (t && matchMedia('(hover:hover)').matches) t.focus({ preventScroll: true }) }
  }
  function onEvent(m, ev) {
    if (ev.type === 'state') { m.state = ev.state; m.pct = ev.pct }
    else if (ev.type === 'found') m.steps.push({ name: 'found', count: ev.count });
    else if (ev.type === 'tool') { if (ev.phase === 'start') m.steps.push({ name: ev.name, args: ev.args }); else { const s = [...m.steps].reverse().find(x => x.name === ev.name && !x.summary); if (s) s.summary = ev.summary } m.state = 'searching' }
    else if (ev.type === 'sources') m.sources = ev.sources;
    else if (ev.type === 'delta') m.content += ev.text;
    else if (ev.type === 'reset') m.content = '';
    else if (ev.type === 'done') { m.content = ev.answer; m.cited = ev.sources; m.consulted = ev.consulted; m.invalid = ev.invalid; m.latency = ev.latency; m.content = m.content.replace(/\[([SM]\d+)\]/g, (x, k) => (ev.invalid || []).includes(k) ? '' : x) }
    else if (ev.type === 'error') {
      if (ev.error === 'model_unavailable') { m.offline = true; m.error = ev.text; fallback(m, (cur.messages[cur.messages.length - 2] || {}).content || '') }
      else { m.error = ev.text; if (ev.sources) m.fallback = ev.sources.slice(0, 8) }
    }
    else if (ev.type === 'stopped') m.stopped = true;
  }
  async function fallback(m, q) {
    try { const d = await api('/api/retrieve?q=' + encodeURIComponent(q) + '&k=8'); m.fallback = d.results.map((r, i) => ({ ...r, key: '' })); if (!m.fallback.length) m.error += ' The library search also found nothing for this question.' }
    catch { m.fallback = [] }
  }
  function stop() { if (cur && live[cur.id]) live[cur.id].abort() }
  function setBusy(on) {
    const b = $('#sendBtn'); if (!b) return;
    b.innerHTML = on ? '<span class="stopsq"></span>' : ico('up');
    b.setAttribute('aria-label', on ? 'Stop the answer' : 'Send');
    b.classList.toggle('stop', on);
  }
  function scrollEnd(force) {
    const near = innerHeight + scrollY > document.body.scrollHeight - 260;
    if (force || near) requestAnimationFrame(() => scrollTo(0, document.body.scrollHeight));
  }
  function throttle(f, ms) { let t = 0, p = null; return () => { const n = Date.now(); if (n - t > ms) { t = n; f() } else { clearTimeout(p); p = setTimeout(() => { t = Date.now(); f() }, ms) } } }

  return { page, count };
})();
