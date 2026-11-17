(() => {
  const state = {
    guestMode: 'SOLO_HOST',
    sessionId: '',
    questions: [],
    currentIndex: 0,
    answersByQid: {},
    notesByQid: {},
    seenQuestionIds: [],
    manifesto: null,
    approvalToken: '',
    remixSeed: 0,
  };

  const el = (id) => document.getElementById(id);

  function switchTab(tab) {
    el('creator-view').classList.toggle('hidden', tab !== 'creator');
    el('wall-view').classList.toggle('hidden', tab !== 'wall');
    el('tab-creator').classList.toggle('active', tab === 'creator');
    el('tab-wall').classList.toggle('active', tab === 'wall');
    if (tab === 'wall') {
      void loadWall();
    }
  }

  function showStage(stage) {
    el('stage-checkin').classList.toggle('hidden', stage !== 'checkin');
    el('stage-quiz').classList.toggle('hidden', stage !== 'quiz');
    el('stage-manifesto').classList.toggle('hidden', stage !== 'manifesto');
  }

  document.querySelectorAll('.mode-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.guestMode = btn.dataset.mode;
      document.querySelectorAll('.mode-btn').forEach((b) => b.classList.remove('selected'));
      btn.classList.add('selected');
      el('plus-one-field').classList.toggle('hidden', state.guestMode !== 'DUO_PAIR');
    });
  });

  el('tab-creator').addEventListener('click', () => switchTab('creator'));
  el('tab-wall').addEventListener('click', () => switchTab('wall'));

  async function startQuiz() {
    const guestName = el('guest-name-input').value.trim() || 'Social Guest';
    const plusOneName = el('plus-one-input').value.trim();
    const res = await fetch('/api/quiz/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        guest_name: guestName,
        plus_one_name: plusOneName,
        guest_mode: state.guestMode,
        excluded_question_ids: state.seenQuestionIds,
      }),
    });
    const data = await res.json();
    state.sessionId = data.session_id;
    state.questions = data.questions;
    state.currentIndex = 0;
    state.answersByQid = {};
    state.notesByQid = {};
    state.remixSeed = 0;
    data.questions.forEach((q) => {
      if (!state.seenQuestionIds.includes(q.question_id)) {
        state.seenQuestionIds.push(q.question_id);
      }
    });
    if (state.seenQuestionIds.length > 20) {
      state.seenQuestionIds = state.seenQuestionIds.slice(-10);
    }
    renderQuestion();
    showStage('quiz');
  }

  function renderQuestion() {
    const q = state.questions[state.currentIndex];
    el('step-counter').textContent = `Question ${state.currentIndex + 1} of ${state.questions.length}`;
    el('dimension-badge').textContent = q.dimension_label;
    el('quiz-progress').value = Math.round(((state.currentIndex + 1) / state.questions.length) * 100);
    el('question-prompt').textContent = q.prompt;
    el('custom-note-input').placeholder = q.custom_placeholder;
    el('custom-note-input').value = state.notesByQid[q.question_id] || '';

    const container = el('options-container');
    container.replaceChildren();
    const selectedId = state.answersByQid[q.question_id] || '';
    q.options.forEach((opt) => {
      const card = document.createElement('button');
      card.type = 'button';
      card.className = 'option-card' + (selectedId === opt.option_id ? ' selected' : '');
      const title = document.createElement('strong');
      title.textContent = `${opt.emoji_badge} ${opt.label}`;
      const sub = document.createElement('span');
      sub.className = 'muted';
      sub.textContent = opt.flavor_subtext;
      card.append(title, sub);
      card.addEventListener('click', () => {
        state.answersByQid[q.question_id] = opt.option_id;
        renderQuestion();
      });
      container.appendChild(card);
    });

    const isLast = state.currentIndex === state.questions.length - 1;
    el('next-question-btn').disabled = !state.answersByQid[q.question_id];
    el('next-question-btn').textContent = isLast
      ? 'Reveal My 2027 Manifesto ✨'
      : 'Next Question →';
  }

  el('custom-note-input').addEventListener('input', (ev) => {
    const q = state.questions[state.currentIndex];
    if (q) {
      state.notesByQid[q.question_id] = ev.target.value;
    }
  });

  el('swap-question-btn').addEventListener('click', async () => {
    const q = state.questions[state.currentIndex];
    const res = await fetch('/api/quiz/swap', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        dimension: q.dimension,
        excluded_question_ids: state.seenQuestionIds,
      }),
    });
    const data = await res.json();
    state.questions[state.currentIndex] = data.question;
    state.seenQuestionIds.push(data.question.question_id);
    renderQuestion();
  });

  el('prev-question-btn').addEventListener('click', () => {
    if (state.currentIndex > 0) {
      state.currentIndex -= 1;
      renderQuestion();
    } else {
      showStage('checkin');
    }
  });

  el('next-question-btn').addEventListener('click', async () => {
    if (state.currentIndex < state.questions.length - 1) {
      state.currentIndex += 1;
      renderQuestion();
      return;
    }
    await generateManifesto(0);
  });

  async function generateManifesto(seed) {
    const guestName = el('guest-name-input').value.trim() || 'Social Guest';
    const plusOneName = el('plus-one-input').value.trim();
    const answers = state.questions.map((q) => ({
      question_id: q.question_id,
      selected_option_id: state.answersByQid[q.question_id],
      custom_note: state.notesByQid[q.question_id] || '',
    }));
    const res = await fetch('/api/manifesto/generate', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: state.sessionId,
        guest_name: guestName,
        plus_one_name: plusOneName,
        guest_mode: state.guestMode,
        answers,
        remix_seed: seed,
      }),
    });
    const data = await res.json();
    state.manifesto = data.manifesto;
    state.approvalToken = data.hitl_publish_approval_token;
    state.remixSeed = seed;
    renderManifesto(data.manifesto);
    showStage('manifesto');
  }

  function renderManifesto(m) {
    el('manifesto-guest').textContent = `Curated for ${m.guest_header}`;
    el('manifesto-persona').textContent = m.persona_title;
    el('manifesto-subtitle').textContent = m.persona_subtitle;
    el('manifesto-motto').textContent = `“${m.headline_motto}”`;
    el('manifesto-forecast').textContent = m.personalized_forecast;

    const metersEl = el('meters-container');
    metersEl.replaceChildren();
    m.energy_meters.forEach((meter) => {
      const box = document.createElement('div');
      box.className = 'sub-card';
      const label = document.createElement('strong');
      label.textContent = `${meter.label}: ${meter.percentage}%`;
      const prog = document.createElement('progress');
      prog.max = 100;
      prog.value = meter.percentage;
      box.append(label, prog);
      metersEl.appendChild(box);
    });

    const resEl = el('resolutions-container');
    resEl.replaceChildren();
    m.resolutions.forEach((r) => {
      const card = document.createElement('article');
      card.className = 'sub-card';
      const cat = document.createElement('div');
      cat.className = 'kicker';
      cat.textContent = r.category;
      const h3 = document.createElement('h3');
      h3.textContent = r.title;
      const p = document.createElement('p');
      p.textContent = r.personalized_story;
      const step = document.createElement('p');
      step.className = 'muted';
      step.textContent = `January First Step: ${r.january_first_step}`;
      card.append(cat, h3, p, step);
      resEl.appendChild(card);
    });

    el('anti-title').textContent = m.anti_resolution_title;
    el('anti-body').textContent = m.anti_resolution_body;
    el('toast-body').textContent = `“${m.celebration_toast}”`;
    el('charm-body').textContent = `Lucky 2027 Charm: ${m.lucky_2027_charm}`;
    el('status-note').classList.add('hidden');
  }

  // HITL confirmation workflow before publishing publicly
  el('publish-wall-btn').addEventListener('click', async () => {
    const preflight = await fetch('/api/wall/publish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: state.sessionId,
        manifesto_id: state.manifesto.manifesto_id,
        human_confirmed: false,
      }),
    });
    const gate = await preflight.json();
    if (gate.status === 'requires_human_confirmation') {
      state.approvalToken = gate.approval_token;
      el('hitl-prompt-text').textContent = gate.confirmation_prompt;
      el('hitl-dialog').showModal();
    }
  });

  el('hitl-cancel-btn').addEventListener('click', () => {
    el('hitl-dialog').close();
  });

  el('hitl-confirm-btn').addEventListener('click', async () => {
    el('hitl-dialog').close();
    const res = await fetch('/api/wall/publish', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        session_id: state.sessionId,
        manifesto_id: state.manifesto.manifesto_id,
        human_confirmed: true,
        approval_token: state.approvalToken,
      }),
    });
    const out = await res.json();
    if (out.status === 'published') {
      const note = el('status-note');
      note.textContent = 'Published to the Celebration Wall with your confirmation!';
      note.classList.remove('hidden');
    }
  });

  el('remix-btn').addEventListener('click', () => {
    void generateManifesto(state.remixSeed + 1);
  });

  el('next-guest-btn').addEventListener('click', () => {
    el('guest-name-input').value = '';
    el('plus-one-input').value = '';
    showStage('checkin');
  });

  async function loadWall() {
    const res = await fetch('/api/wall');
    const data = await res.json();
    el('wall-stats').textContent = `${data.total_published_count} Manifestos · Top Persona: ${data.top_persona_tonight}`;
    const grid = el('wall-grid');
    grid.replaceChildren();
    data.recent_manifestos.forEach((item) => {
      const card = document.createElement('article');
      card.className = 'sub-card';
      const h3 = document.createElement('h3');
      h3.textContent = item.guest_header;
      const persona = document.createElement('div');
      persona.className = 'kicker';
      persona.textContent = item.persona_title;
      const motto = document.createElement('p');
      motto.textContent = `“${item.headline_motto}”`;
      card.append(persona, h3, motto);
      grid.appendChild(card);
    });
  }

  el('deal-questions-btn').addEventListener('click', () => {
    void startQuiz();
  });
})();
