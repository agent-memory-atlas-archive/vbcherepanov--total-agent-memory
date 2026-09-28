'use strict';
(() => {
  const API = '/learning/api/';
  const STATUS = {
    not_started: ['Not started', ''], opened: ['Opened', 'violet'], in_progress: ['In progress', 'violet'],
    quiz_available: ['Quiz ready', 'accent'], pending_review: ['Awaiting review', 'warn'], passed: ['Passed', 'good'],
    failed: ['Failed', 'bad'], completed: ['Completed', 'good'],
  };
  const NEXT = {
    lesson: 'Next lesson', review: 'Review updated lesson', quiz: 'Quiz ready', wait_review: 'Waiting for grading',
    contact_manager: 'Ask your department head', done: 'All done',
  };
  const TYPES = {single: 'Single choice', multiple: 'Multiple choice', open: 'Open answer'};
  const LIMITS = {title: 200, text: 20000, modules: 100, lessons: 100, records: 50, questions: 100, options: 10,
    points: 100, attempts: 20};
  const DEFAULTS = {threshold: 80, attempts: 3};
  const RING = {size: 64, stroke: 7};
  const OVERSIGHT = ['company_viewer', 'superadmin'];
  const SEARCH_LIMIT = 20;
  const BROWSE_LIMIT = 50;
  let uid = 0;
  const nextId = (prefix) => prefix + '-' + (++uid);

  const post = (ctx, action, body = {}) => ctx.api(API + action, {method: 'POST', body});
  const percent = (value) => (value === null || value === undefined ? '—' : value + '%');
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);

  function statusChip(ui, status) {
    const [label, tone] = STATUS[status] || [status, ''];
    return ui.chip(label, tone, {dot: true});
  }

  function ring(ui, value, label) {
    const r = (RING.size - RING.stroke) / 2;
    const c = 2 * Math.PI * r;
    const done = Math.max(0, Math.min(100, value));
    return ui.h('div', {class: 'tl-ring', role: 'img', 'aria-label': label + ': ' + done + '%'},
      ui.svg('svg', {viewBox: '0 0 ' + RING.size + ' ' + RING.size, width: RING.size, height: RING.size, 'aria-hidden': 'true'},
        ui.svg('circle', {class: 'track', cx: RING.size / 2, cy: RING.size / 2, r, 'stroke-width': RING.stroke}),
        done === 0 ? null : ui.svg('circle', {class: 'value', cx: RING.size / 2, cy: RING.size / 2, r, 'stroke-width': RING.stroke,
          'stroke-dasharray': (c * done / 100).toFixed(2) + ' ' + c.toFixed(2),
          transform: 'rotate(-90 ' + RING.size / 2 + ' ' + RING.size / 2 + ')'})),
      ui.h('span', {class: 'num', text: done + '%'}));
  }

  function bar(ui, value, label) {
    const done = Math.max(0, Math.min(100, value || 0));
    return ui.h('div', {class: 'tl-bar', role: 'img', 'aria-label': label + ': ' + done + '%'},
      ui.svg('svg', {viewBox: '0 0 100 8', preserveAspectRatio: 'none', 'aria-hidden': 'true'},
        ui.svg('rect', {class: 'track', x: 0, y: 0, width: 100, height: 8, rx: 4}),
        ui.svg('rect', {class: 'fill', x: 0, y: 0, width: done ? Math.max(done, 3) : 0, height: 8, rx: 4})),
      ui.h('span', {class: 'num', text: done + '%'}));
  }

  function logFeed(ui, entries, emptyText) {
    if (!entries.length) return ui.empty({icon: 'history', text: emptyText});
    return ui.h('ul', {class: 'feed tl-log'}, entries.map((entry) => ui.h('li', {title: entry.summary},
      ui.h('span', {class: 'op insert', 'aria-hidden': 'true'}, ui.icon(entry.event.startsWith('quiz') ? 'check' : 'book', 'sm')),
      ui.h('div', {class: 'what'}, ui.h('p', {text: entry.summary})), ui.time(entry.at))));
  }

  function quizLine(ui, quiz) {
    if (!quiz) return ui.h('span', {class: 'muted', text: 'No quiz'});
    const parts = [];
    if (quiz.best_percent !== null) parts.push('best ' + quiz.best_score + '/' + quiz.max_score + ' (' + quiz.best_percent + '%)');
    if (quiz.pending_review) parts.push('awaiting grading');
    parts.push(quiz.attempts_used + '/' + quiz.max_attempts + ' attempts');
    parts.push('pass at ' + Math.round(quiz.pass_threshold * 100) + '%');
    return ui.h('span', {class: 'muted', text: parts.join(' · ')});
  }

  // ---------- Employee: my courses ----------

  function moduleBlock(ui, module) {
    const lessons = ui.h('ol', {class: 'tl-lessons'}, module.lessons.map((lesson) => ui.h('li', {class: 'tl-lesson-row'},
      ui.h('span', {class: 'tl-mark ' + lesson.status, 'aria-hidden': 'true'},
        ui.icon(lesson.status === 'completed' ? 'check' : lesson.status === 'opened' ? 'clock' : 'book', 'sm')),
      ui.h('div', {class: 'tl-lesson-text'}, ui.h('strong', {text: lesson.title}),
        ui.h('small', {class: 'muted'}, lesson.completed_at ? ['Completed ', ui.time(lesson.completed_at)]
          : lesson.opened_at ? ['Opened ', ui.time(lesson.opened_at)] : 'Not opened yet')),
      lesson.updated_since_studied ? ui.chip('Updated since you studied', 'warn', {dot: true}) : statusChip(ui, lesson.status))));
    const quiz = module.quiz ? ui.h('div', {class: 'tl-quiz'}, ui.icon('check', 'sm'), ui.h('strong', {text: 'Quiz'}),
      quizLine(ui, module.quiz), module.quiz.passed_at ? ui.h('span', {class: 'muted'}, 'passed ', ui.time(module.quiz.passed_at)) : null) : null;
    const updated = module.lessons.filter((lesson) => lesson.updated_since_studied).length;
    const finished = module.status === 'passed' || module.status === 'completed';
    return ui.h('details', {class: 'tl-module', open: !finished || updated > 0},
      ui.h('summary', {},
        ui.h('span', {class: 'tl-module-title'}, ui.h('strong', {text: module.title}),
          ui.h('small', {class: 'muted', text: module.lessons_completed + '/' + module.lessons_total + ' lessons'})),
        ui.h('span', {class: 'actions'}, updated ? ui.chip(updated + ' updated', 'warn') : null, statusChip(ui, module.status))),
      ui.h('div', {class: 'tl-module-body'}, lessons, quiz));
  }

  async function course(ctx, ui, team) {
    if (!team.has_curriculum) {
      return ui.card({title: team.name, subtitle: 'Onboarding'}, ui.empty({icon: 'book', title: 'No curriculum yet',
        text: 'Your department head has not published onboarding material for ' + team.name + ' yet.'}));
    }
    const progress = await post(ctx, 'progress', {team_id: team.team_id});
    const summary = progress.summary;
    const step = progress.next;
    const command = '/onboard ' + team.team_id;
    const head = ui.h('div', {class: 'tl-course-head'},
      ring(ui, summary.percent, 'Lessons completed in ' + team.name),
      ui.h('div', {class: 'tl-course-stats'},
        ui.h('strong', {text: plural(summary.lessons_completed, 'lesson', 'lessons') + ' of ' + summary.lessons_total}),
        ui.h('span', {class: 'muted', text: summary.modules_finished + '/' + summary.modules_total + ' modules finished'}),
        summary.updated_lessons ? ui.chip(plural(summary.updated_lessons, 'lesson', 'lessons') + ' updated since you studied', 'warn', {dot: true}) : null),
      ui.h('div', {class: 'tl-next'},
        ui.h('small', {class: 'muted', text: NEXT[step.action] || 'Next'}),
        ui.h('strong', {text: step.title || step.hint}),
        ui.h('div', {class: 'actions'}, ui.h('code', {class: 'tl-command', text: command}), ui.copyButton(command)),
        ui.h('small', {class: 'muted', text: 'Continue in your agent (Claude Code, Codex): type the command above.'})));
    return ui.card({title: progress.title, subtitle: team.name + (progress.enrolled_at ? ' · started ' + ui.relative(progress.enrolled_at) : ' · not started yet')},
      head, ui.h('div', {class: 'tl-modules'}, progress.modules.map((m) => moduleBlock(ui, m))),
      progress.log.length ? ui.h('details', {class: 'tl-history'}, ui.h('summary', {text: 'My learning history'}),
        logFeed(ui, progress.log.slice(0, 10), 'Nothing yet.')) : null);
  }

  async function myCourses(ctx, ui, overview) {
    if (!overview.teams.length) {
      return ui.empty({icon: 'book', title: 'You are not in a department yet',
        text: 'Onboarding appears here once a superadmin adds you to a department.'});
    }
    return Promise.all(overview.teams.map((team) => course(ctx, ui, team)));
  }

  // ---------- Manager / viewer: department ----------

  function matrix(ui, report) {
    const columns = [
      {title: 'Employee', render: (m) => ui.person(m.name, m.user_id)},
      {title: 'Overall', render: (m) => m.enrolled_at ? bar(ui, m.summary.percent, m.name + ' lessons completed')
        : ui.h('span', {class: 'muted', text: 'Not started'})},
      ...report.modules.map((module, index) => ({title: module.title, className: 'tl-cell', render: (m) => {
        const cell = m.modules[index];
        const quiz = cell.quiz;
        const date = quiz && quiz.passed_at ? quiz.passed_at : cell.lessons_finished_at || cell.started_at;
        const facts = [cell.lessons_completed + '/' + cell.lessons_total];
        if (quiz && quiz.best_percent !== null) facts.push(quiz.best_percent + '%');
        return ui.h('div', {class: 'tl-cell-body'}, statusChip(ui, cell.status),
          ui.h('small', {class: 'muted'}, facts.join(' · '), date ? [' · ', ui.time(date)] : null));
      }})),
    ];
    return ui.table(columns, report.members, ui.empty({icon: 'users', title: 'No members yet',
      text: 'Add people to this department under Administration → Departments.'}));
  }

  function gradingQueue(ctx, ui, report, reload) {
    const titles = Object.fromEntries(report.modules.map((m) => [m.module_id, m.title]));
    const names = Object.fromEntries(report.members.map((m) => [m.user_id, m.name]));
    if (!report.grading_queue.length) return ui.empty({icon: 'check', title: 'Nothing to grade', text: 'Open answers that need your review appear here.'});
    return ui.h('div', {class: 'grid'}, report.grading_queue.map((item) => {
      const pointsId = nextId('points');
      const points = ui.h('input', {id: pointsId, type: 'number', min: '0', max: String(item.points), step: '0.5', required: true, inputmode: 'decimal'});
      const comment = ui.h('input', {type: 'text', maxlength: '2000', placeholder: 'Feedback for the employee (optional)'});
      const submit = ui.h('button', {type: 'button', class: 'btn primary sm'}, ui.icon('check', 'sm'), 'Save grade');
      submit.addEventListener('click', async () => {
        const value = Number(points.value);
        if (points.value === '' || Number.isNaN(value) || value < 0 || value > item.points) {
          points.setAttribute('aria-invalid', 'true');
          ui.toast('Enter points between 0 and ' + item.points, 'bad');
          return;
        }
        points.removeAttribute('aria-invalid');
        submit.disabled = true;
        try {
          const result = await post(ctx, 'grade', {attempt_id: item.attempt_id, question_id: item.question_id, score: value, comment: comment.value});
          ui.toast(result.status === 'graded' ? 'Attempt graded: ' + result.percent + '%' + (result.passed ? ', passed' : ', not passed') : 'Grade saved', 'good');
          reload();
        } catch (error) {
          ui.toast(error.message, 'bad');
          submit.disabled = false;
        }
      });
      return ui.h('article', {class: 'tl-grade'},
        ui.h('div', {class: 'record-meta'}, ui.person(names[item.user_id] || item.user_id, item.user_id),
          ui.chip(titles[item.module_id] || item.module_id, 'violet'), ui.h('span', {}, 'submitted ', ui.time(item.submitted_at))),
        ui.h('p', {class: 'tl-prompt', text: item.prompt}),
        ui.h('p', {class: 'muted'}, ui.h('strong', {text: 'Rubric: '}), item.rubric),
        ui.h('blockquote', {class: 'tl-answer', text: item.answer}),
        ui.h('div', {class: 'form-grid'}, ui.field('Points (max ' + item.points + ')', points), ui.field('Comment', comment),
          ui.h('div', {class: 'actions'}, submit)));
    }));
  }

  async function department(ctx, ui, overview, teamId, host) {
    const manage = overview.manageable_teams.some((t) => t.team_id === teamId);
    const team = overview.viewable_teams.find((t) => t.team_id === teamId);
    const kpis = await ctx.api(API + 'kpis/team/' + encodeURIComponent(teamId));
    const report = kpis.has_curriculum ? await post(ctx, 'team_report', {team_id: teamId}) : null;
    const reload = () => ui.load(host, () => department(ctx, ui, overview, teamId, host));
    const nodes = [!kpis.has_curriculum ? null : ui.h('div', {class: 'kpis'},
      ui.kpi({label: 'Completion', value: percent(kpis.completion_percent), tone: 'accent',
        sub: 'average share of lessons completed'}),
      ui.kpi({label: 'Finished onboarding', value: kpis.finished + ' / ' + kpis.members, sub: kpis.enrolled + ' enrolled'}),
      ui.kpi({label: 'Average quiz score', value: percent(kpis.average_score_percent), sub: kpis.modules + ' modules'}),
      ui.kpi({label: 'Awaiting grading', value: String(kpis.pending_grading), tone: kpis.pending_grading ? 'warn' : '',
        sub: kpis.pending_grading ? 'Open answers need review' : 'Queue is empty'}))];
    if (report) {
      nodes.push(ui.card({title: 'Progress by module', subtitle: report.title + ' · ' + team.name, flush: true}, matrix(ui, report)));
      const side = [ui.card({title: 'Learning log'}, logFeed(ui, report.log.slice(0, 12), 'No activity yet.'))];
      if (report.grading_queue) side.unshift(ui.card({title: 'Grading queue', subtitle: plural(report.grading_queue.length, 'open answer', 'open answers')}, gradingQueue(ctx, ui, report, reload)));
      nodes.push(ui.h('div', {class: 'split'}, side));
    } else {
      nodes.push(ui.card({title: team.name}, ui.empty({icon: 'book', title: 'No curriculum yet',
        text: manage ? 'Build one below: start from a draft of your team memory, then edit and publish.'
          : 'The department head has not published a curriculum yet.'})));
    }
    if (manage) nodes.push(...await editors(ctx, ui, teamId, team.name, reload));
    return nodes;
  }

  // ---------- Curriculum editor ----------

  function mover(ui, list, index, redraw, label) {
    const move = (delta) => {
      const target = index + delta;
      if (target < 0 || target >= list.length) return;
      [list[index], list[target]] = [list[target], list[index]];
      redraw();
    };
    return ui.h('div', {class: 'row-actions'},
      ui.h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-label': 'Move ' + label + ' up', disabled: index === 0, onclick: () => move(-1)}, ui.icon('arrow-up', 'sm')),
      ui.h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-label': 'Move ' + label + ' down', disabled: index === list.length - 1, onclick: () => move(1)}, ui.icon('arrow-down', 'sm')),
      ui.h('button', {type: 'button', class: 'btn ghost sm icon-only danger', 'aria-label': 'Remove ' + label, onclick: async () => {
        if (await ui.confirm('Remove ' + label + '?', 'It disappears from the editor. Published progress stays until you publish again.', 'Remove')) {
          list.splice(index, 1);
          redraw();
        }
      }}, ui.icon('trash', 'sm')));
  }

  function bound(ui, tag, attrs, get, set) {
    const node = ui.h(tag, attrs);
    node.value = get();
    node.addEventListener('input', () => { set(node.value); node.removeAttribute('aria-invalid'); });
    return node;
  }

  function validateCurriculum(model) {
    const errors = [];
    if (!model.title.trim()) errors.push(['title', 'Curriculum title is required']);
    if (!model.modules.length) errors.push(['modules', 'Add at least one module']);
    model.modules.forEach((m, i) => {
      const where = 'Module ' + (i + 1);
      if (!m.title.trim()) errors.push([m.key + ':title', where + ': title is required']);
      const threshold = Number(m.threshold);
      if (!(threshold >= 1 && threshold <= 100)) errors.push([m.key + ':threshold', where + ': pass threshold must be 1–100%']);
      const attempts = Number(m.attempts);
      if (!(Number.isInteger(attempts) && attempts >= 1 && attempts <= LIMITS.attempts)) errors.push([m.key + ':attempts', where + ': attempts must be 1–' + LIMITS.attempts]);
      if (!m.lessons.length) errors.push([m.key + ':lessons', where + ': add at least one lesson']);
      m.lessons.forEach((l, j) => {
        const lw = where + ', lesson ' + (j + 1);
        if (!l.title.trim()) errors.push([l.key + ':title', lw + ': title is required']);
        if (!l.body.trim() && !l.record_ids.length) errors.push([l.key + ':body', lw + ': add text or at least one source record']);
        if (l.record_ids.length > LIMITS.records) errors.push([l.key + ':body', lw + ': at most ' + LIMITS.records + ' source records']);
      });
    });
    return errors;
  }

  function toModel(data, fallbackTitle) {
    const curriculum = data && data.curriculum;
    const excerpts = {};
    const modules = (curriculum ? curriculum.modules : []).map((m) => ({
      key: nextId('m'), id: m.id, title: m.title, summary: m.summary || '',
      threshold: Math.round((m.pass_threshold ?? DEFAULTS.threshold / 100) * 100), attempts: m.max_attempts ?? DEFAULTS.attempts,
      lessons: m.lessons.map((l) => {
        for (const source of l.sources || []) excerpts[source.requested_id] = source.missing ? null : source.excerpt;
        return {key: nextId('l'), id: l.id, title: l.title, body: l.body || '', record_ids: [...l.record_ids]};
      }),
    }));
    return {revision: data ? data.revision : 0, title: curriculum ? curriculum.title : fallbackTitle, modules, excerpts};
  }

  function fromDraft(model, draft) {
    model.title = draft.title;
    model.modules = draft.modules.map((m) => ({key: nextId('m'), id: null, title: m.title, summary: m.summary || '',
      threshold: DEFAULTS.threshold, attempts: DEFAULTS.attempts,
      lessons: m.lessons.map((l) => ({key: nextId('l'), id: null, title: l.title, body: l.body || '', record_ids: [...l.record_ids]}))}));
  }

  function toPayload(model) {
    return {title: model.title.trim(), modules: model.modules.map((m) => ({
      ...(m.id ? {id: m.id} : {}), title: m.title.trim(), summary: m.summary, pass_threshold: Number(m.threshold) / 100,
      max_attempts: Number(m.attempts), lessons: m.lessons.map((l) => ({...(l.id ? {id: l.id} : {}), title: l.title.trim(),
        body: l.body, record_ids: l.record_ids}))}))};
  }

  async function pickSources(ctx, ui, teamId, lesson, excerpts) {
    const query = ui.h('input', {type: 'search', placeholder: 'Search team memory', 'aria-label': 'Search team memory'});
    const results = ui.h('div', {class: 'tl-picker', role: 'group', 'aria-label': 'Records'});
    const chosen = new Map();
    const scope = {kind: 'team', team_id: teamId};
    const show = (records) => {
      if (!records.length) { results.replaceChildren(ui.empty({icon: 'search', text: 'No records found.'})); return; }
      results.replaceChildren(...records.map((record) => {
        const box = ui.h('input', {type: 'checkbox', checked: lesson.record_ids.includes(record.id) || chosen.has(record.id),
          disabled: lesson.record_ids.includes(record.id)});
        box.addEventListener('change', () => { if (box.checked) chosen.set(record.id, record); else chosen.delete(record.id); });
        return ui.h('label', {class: 'tl-pick'}, box,
          ui.h('span', {class: 'tl-pick-text'}, ui.h('span', {class: 'record-meta'}, ui.h('span', {class: 'num', text: '#' + record.id}),
            record.type ? ui.chip(record.type, 'violet') : null, record.project && record.project !== 'general' ? ui.chip(record.project) : null),
          ui.h('span', {text: (record.content || '').slice(0, 220)})));
      }));
    };
    const run = async () => {
      results.replaceChildren(ui.skeleton(3));
      try {
        if (query.value.trim()) {
          const found = await ctx.api('memory', {method: 'POST', body: {name: 'memory_recall',
            arguments: {query: query.value.trim(), scope, limit: SEARCH_LIMIT}}});
          show(found.results.map((item) => item.record));
        } else {
          const page = await ctx.api('memory', {method: 'POST', body: {name: 'memory_export', arguments: {scope, after: 0, limit: BROWSE_LIMIT}}});
          show(page.data.filter((record) => record.status === 'active'));
        }
      } catch (error) {
        results.replaceChildren(ui.empty({icon: 'alert', title: 'Search failed', text: error.message}));
      }
    };
    query.addEventListener('keydown', (event) => { if (event.key === 'Enter') { event.preventDefault(); run(); } });
    const searchButton = ui.h('button', {type: 'button', class: 'btn sm', onclick: run}, ui.icon('search', 'sm'), 'Search');
    const body = [ui.h('p', {class: 'muted', text: 'Search to find records, or leave empty and search to browse the latest ones.'}),
      ui.h('div', {class: 'tl-picker-bar'}, ui.h('div', {class: 'search'}, ui.icon('search', 'sm'), query), searchButton), results];
    run();
    const confirmed = await ui.modal({title: 'Add sources to “' + (lesson.title || 'lesson') + '”', body, confirm: 'Add selected'});
    if (!confirmed) return false;
    for (const [id, record] of chosen) {
      if (lesson.record_ids.length >= LIMITS.records) { ui.toast('A lesson can reference at most ' + LIMITS.records + ' records', 'bad'); break; }
      if (!lesson.record_ids.includes(id)) lesson.record_ids.push(id);
      excerpts[id] = (record.content || '').split('\n').find((line) => line.trim()) || '';
    }
    return chosen.size > 0;
  }

  function curriculumEditor(ctx, ui, teamId, teamName, data, onSaved) {
    const model = toModel(data, teamName + ' onboarding');
    const body = ui.h('div', {class: 'grid'});
    const alertBox = ui.h('div', {class: 'tl-errors', role: 'alert', hidden: true});
    const revision = ui.chip('revision ' + model.revision, '');
    const fields = new Map();

    function track(key, node) { fields.set(key, node); return node; }

    function sourceList(lesson, redraw) {
      const chips = lesson.record_ids.map((id, index) => {
        const excerpt = model.excerpts[id];
        return ui.h('li', {class: 'tl-source' + (excerpt === null ? ' missing' : '')},
          ui.h('span', {class: 'num', text: '#' + id}),
          ui.h('span', {class: 'tl-source-text', text: excerpt === null ? 'Record no longer exists' : excerpt || 'Saved record'}),
          ui.h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-label': 'Remove source #' + id,
            onclick: () => { lesson.record_ids.splice(index, 1); redraw(); }}, ui.icon('x', 'sm')));
      });
      return ui.h('div', {class: 'tl-sources'},
        chips.length ? ui.h('ul', {class: 'tl-source-list'}, chips) : ui.h('small', {class: 'muted', text: 'No source records yet.'}),
        ui.h('button', {type: 'button', class: 'btn sm', onclick: async () => {
          if (await pickSources(ctx, ui, teamId, lesson, model.excerpts)) redraw();
        }}, ui.icon('plus', 'sm'), 'Add sources'));
    }

    function lessonBlock(module, lesson, index, redraw) {
      return ui.h('div', {class: 'tl-block tl-lesson-edit'},
        ui.h('div', {class: 'tl-block-head'}, ui.h('span', {class: 'tl-step', text: 'Lesson ' + (index + 1)}),
          lesson.id ? ui.h('small', {class: 'muted num', text: lesson.id}) : ui.chip('new', 'accent'),
          mover(ui, module.lessons, index, redraw, 'lesson ' + (index + 1))),
        ui.field('Title', track(lesson.key + ':title', bound(ui, 'input', {type: 'text', maxlength: String(LIMITS.title), required: true},
          () => lesson.title, (v) => { lesson.title = v; })) ),
        ui.field('Lesson text', track(lesson.key + ':body', bound(ui, 'textarea', {rows: '3', maxlength: String(LIMITS.text)},
          () => lesson.body, (v) => { lesson.body = v; })), 'What the employee should learn; the agent teaches it together with the sources.'),
        ui.h('div', {class: 'field'}, ui.h('span', {text: 'Source records'}), sourceList(lesson, redraw)));
    }

    function moduleBlockEdit(module, index, redraw) {
      const lessons = module.lessons.map((lesson, j) => lessonBlock(module, lesson, j, redraw));
      return ui.h('fieldset', {class: 'tl-block tl-module-edit'},
        ui.h('legend', {class: 'sr-only', text: 'Module ' + (index + 1)}),
        ui.h('div', {class: 'tl-block-head'}, ui.h('span', {class: 'tl-step accent', text: 'Module ' + (index + 1)}),
          module.id ? ui.h('small', {class: 'muted num', text: module.id}) : ui.chip('new', 'accent'),
          mover(ui, model.modules, index, redraw, 'module ' + (index + 1))),
        ui.h('div', {class: 'form-grid'},
          ui.field('Module title', track(module.key + ':title', bound(ui, 'input', {type: 'text', maxlength: String(LIMITS.title), required: true},
            () => module.title, (v) => { module.title = v; }))),
          ui.field('Quiz pass threshold, %', track(module.key + ':threshold', bound(ui, 'input', {type: 'number', min: '1', max: '100', step: '1', inputmode: 'numeric'},
            () => module.threshold, (v) => { module.threshold = v; }))),
          ui.field('Quiz attempts', track(module.key + ':attempts', bound(ui, 'input', {type: 'number', min: '1', max: String(LIMITS.attempts), step: '1', inputmode: 'numeric'},
            () => module.attempts, (v) => { module.attempts = v; })))),
        ui.field('Summary', bound(ui, 'textarea', {rows: '2', maxlength: String(LIMITS.text)}, () => module.summary, (v) => { module.summary = v; })),
        track(module.key + ':lessons', ui.h('div', {class: 'tl-lesson-list'}, lessons)),
        ui.h('div', {class: 'actions'}, ui.h('button', {type: 'button', class: 'btn sm', disabled: module.lessons.length >= LIMITS.lessons, onclick: () => {
          module.lessons.push({key: nextId('l'), id: null, title: '', body: '', record_ids: []});
          redraw();
        }}, ui.icon('plus', 'sm'), 'Add lesson')));
    }

    function redraw() {
      fields.clear();
      body.replaceChildren(
        ui.field('Curriculum title', track('title', bound(ui, 'input', {type: 'text', maxlength: String(LIMITS.title), required: true},
          () => model.title, (v) => { model.title = v; }))),
        model.modules.length ? ui.h('div', {class: 'grid'}, model.modules.map((m, i) => moduleBlockEdit(m, i, redraw)))
          : track('modules', ui.empty({icon: 'layers', title: 'No modules yet', text: 'Draft from team memory or add a module by hand.'})),
        ui.h('div', {class: 'actions'}, ui.h('button', {type: 'button', class: 'btn', disabled: model.modules.length >= LIMITS.modules, onclick: () => {
          model.modules.push({key: nextId('m'), id: null, title: '', summary: '', threshold: DEFAULTS.threshold, attempts: DEFAULTS.attempts,
            lessons: [{key: nextId('l'), id: null, title: '', body: '', record_ids: []}]});
          redraw();
        }}, ui.icon('plus', 'sm'), 'Add module')));
    }

    function showErrors(errors) {
      for (const node of fields.values()) node.removeAttribute('aria-invalid');
      if (!errors.length) { alertBox.hidden = true; alertBox.replaceChildren(); return; }
      for (const [key] of errors) fields.get(key)?.setAttribute('aria-invalid', 'true');
      alertBox.hidden = false;
      alertBox.replaceChildren(ui.h('strong', {text: 'Fix before publishing:'}), ui.h('ul', {}, errors.slice(0, 8).map(([, text]) => ui.h('li', {text}))));
      const first = fields.get(errors[0][0]);
      if (first && typeof first.focus === 'function') first.focus();
    }

    const draftButton = ui.h('button', {type: 'button', class: 'btn sm'}, ui.icon('spark', 'sm'), 'Draft from team memory');
    draftButton.addEventListener('click', async () => {
      if (model.modules.length && !await ui.confirm('Replace the editor with a draft?', 'Unpublished edits in the editor are replaced. Nothing is published until you press Publish.', 'Replace', 'primary')) return;
      draftButton.disabled = true;
      try {
        const result = await post(ctx, 'curriculum_draft', {team_id: teamId});
        if (!result.draft) { ui.toast(result.message, 'info'); return; }
        fromDraft(model, result.draft);
        redraw();
        showErrors([]);
        ui.toast('Draft built from ' + plural(result.records_considered, 'record', 'records') + (result.llm_used ? ' (titles polished by the LLM)' : '') + '. Review, then publish.', 'good');
      } catch (error) {
        ui.toast(error.message, 'bad');
      } finally {
        draftButton.disabled = false;
      }
    });
    const publishButton = ui.h('button', {type: 'button', class: 'btn primary sm'}, ui.icon('check', 'sm'), 'Publish');
    publishButton.addEventListener('click', async () => {
      const errors = validateCurriculum(model);
      showErrors(errors);
      if (errors.length) { ui.toast(errors[0][1], 'bad'); return; }
      publishButton.disabled = true;
      try {
        await post(ctx, 'curriculum_set', {team_id: teamId, expected_revision: model.revision, curriculum: toPayload(model)});
        ui.toast('Curriculum published', 'good');
        onSaved();
      } catch (error) {
        showErrors([['title', error.message]]);
        ui.toast(error.message, 'bad');
        publishButton.disabled = false;
      }
    });
    redraw();
    return ui.card({title: 'Curriculum', subtitle: 'Modules and lessons employees study, in order.',
      actions: [revision, draftButton, publishButton]}, alertBox, body);
  }

  // ---------- Quiz editor ----------

  function validateQuiz(questions) {
    const errors = [];
    if (!questions.length) errors.push([null, 'Add at least one question']);
    questions.forEach((q, i) => {
      const where = 'Question ' + (i + 1);
      if (!q.prompt.trim()) errors.push([q.key + ':prompt', where + ': prompt is required']);
      const points = Number(q.points);
      if (!(Number.isInteger(points) && points >= 1 && points <= LIMITS.points)) errors.push([q.key + ':points', where + ': points must be 1–' + LIMITS.points]);
      if (q.type === 'open') {
        if (!q.rubric.trim()) errors.push([q.key + ':rubric', where + ': write a rubric (reference answer)']);
      } else {
        if (q.options.length < 2 || q.options.some((o) => !o.trim())) errors.push([q.key + ':options', where + ': needs at least two non-empty options']);
        if (q.type === 'single' && q.correct.length !== 1) errors.push([q.key + ':options', where + ': mark exactly one correct option']);
        if (q.type === 'multiple' && !q.correct.length) errors.push([q.key + ':options', where + ': mark at least one correct option']);
      }
    });
    return errors;
  }

  function quizEditor(ctx, ui, data, onSaved) {
    const modules = data && data.curriculum ? data.curriculum.modules : [];
    if (!modules.length) {
      return ui.card({title: 'Quizzes'}, ui.empty({icon: 'check', title: 'Publish the curriculum first', text: 'Each module gets its own quiz once the module is published.'}));
    }
    const picker = ui.h('select', {'aria-label': 'Module'}, modules.map((m, i) => ui.h('option', {value: m.id, text: (i + 1) + '. ' + m.title})));
    const body = ui.h('div', {class: 'grid'});
    const alertBox = ui.h('div', {class: 'tl-errors', role: 'alert', hidden: true});
    const info = ui.h('div', {class: 'record-meta tl-quiz-info'});
    const fields = new Map();
    let state = null;

    const track = (key, node) => { fields.set(key, node); return node; };
    const fromQuestion = (q) => ({key: nextId('q'), id: q.id || null, type: q.type, prompt: q.prompt || '', points: q.points || 1,
      options: [...(q.options || [])], correct: [...(q.correct || [])], rubric: q.rubric || '', lesson_id: q.lesson_id || ''});
    const blank = (type) => fromQuestion({type, options: type === 'open' ? [] : ['', ''], correct: []});

    function load() {
      const module = modules.find((m) => m.id === picker.value);
      state = {module, revision: module.quiz ? module.quiz.revision : 0, questions: module.quiz ? module.quiz.questions.map(fromQuestion) : []};
      info.replaceChildren(ui.chip('Pass at ' + Math.round(module.pass_threshold * 100) + '%', 'accent'),
        ui.chip(plural(module.max_attempts, 'attempt', 'attempts'), 'violet'), ui.chip('revision ' + state.revision),
        ui.h('span', {class: 'muted', text: 'Threshold and attempts are set per module in the curriculum.'}));
      alertBox.hidden = true;
      redraw();
    }

    function optionsEditor(q, redraw) {
      const group = 'correct-' + q.key;
      const rows = q.options.map((option, index) => {
        const toggle = ui.h('input', {type: q.type === 'single' ? 'radio' : 'checkbox', name: group, checked: q.correct.includes(index),
          'aria-label': 'Option ' + (index + 1) + ' is correct'});
        toggle.addEventListener('change', () => {
          if (q.type === 'single') q.correct = [index];
          else if (toggle.checked) q.correct = [...new Set([...q.correct, index])].sort((a, b) => a - b);
          else q.correct = q.correct.filter((c) => c !== index);
        });
        const text = bound(ui, 'input', {type: 'text', maxlength: String(LIMITS.text), placeholder: 'Option ' + (index + 1), 'aria-label': 'Option ' + (index + 1) + ' text'},
          () => option, (v) => { q.options[index] = v; });
        return ui.h('div', {class: 'tl-option'},
          ui.h('label', {class: 'check'}, toggle, ui.h('span', {class: 'tl-correct-label', text: 'Correct'})), text,
          ui.h('button', {type: 'button', class: 'btn ghost sm icon-only', 'aria-label': 'Remove option ' + (index + 1), disabled: q.options.length <= 2,
            onclick: () => {
              q.options.splice(index, 1);
              q.correct = q.correct.filter((c) => c !== index).map((c) => (c > index ? c - 1 : c));
              redraw();
            }}, ui.icon('x', 'sm')));
      });
      return track(q.key + ':options', ui.h('div', {class: 'tl-options', role: 'group', 'aria-label': 'Options'}, rows,
        ui.h('div', {class: 'actions'}, ui.h('button', {type: 'button', class: 'btn sm', disabled: q.options.length >= LIMITS.options,
          onclick: () => { q.options.push(''); redraw(); }}, ui.icon('plus', 'sm'), 'Add option'))));
    }

    function questionBlock(q, index, redraw) {
      const type = ui.h('select', {'aria-label': 'Question type'}, Object.entries(TYPES).map(([value, text]) => ui.h('option', {value, text})));
      type.value = q.type;
      type.addEventListener('change', () => {
        q.type = type.value;
        if (q.type === 'open') { q.options = []; q.correct = []; }
        else {
          if (q.options.length < 2) q.options = [...q.options, '', ''].slice(0, Math.max(2, q.options.length));
          if (q.type === 'single') q.correct = q.correct.slice(0, 1);
        }
        redraw();
      });
      const lesson = ui.h('select', {'aria-label': 'Related lesson'}, ui.h('option', {value: '', text: 'Any lesson'}),
        state.module.lessons.map((l) => ui.h('option', {value: l.id, text: l.title})));
      lesson.value = q.lesson_id;
      lesson.addEventListener('change', () => { q.lesson_id = lesson.value; });
      return ui.h('fieldset', {class: 'tl-block'},
        ui.h('legend', {class: 'sr-only', text: 'Question ' + (index + 1)}),
        ui.h('div', {class: 'tl-block-head'}, ui.h('span', {class: 'tl-step accent', text: 'Question ' + (index + 1)}),
          ui.chip(TYPES[q.type], q.type === 'open' ? 'violet' : ''), mover(ui, state.questions, index, redraw, 'question ' + (index + 1))),
        ui.field('Question', track(q.key + ':prompt', bound(ui, 'textarea', {rows: '2', maxlength: String(LIMITS.text)}, () => q.prompt, (v) => { q.prompt = v; }))),
        ui.h('div', {class: 'form-grid'}, ui.field('Type', type), ui.field('Points', track(q.key + ':points',
          bound(ui, 'input', {type: 'number', min: '1', max: String(LIMITS.points), step: '1', inputmode: 'numeric'}, () => q.points, (v) => { q.points = v; }))),
        ui.field('Lesson', lesson)),
        q.type === 'open'
          ? ui.field('Rubric', track(q.key + ':rubric', bound(ui, 'textarea', {rows: '2', maxlength: String(LIMITS.text)}, () => q.rubric, (v) => { q.rubric = v; })),
            'Reference answer. Used by the LLM grader, or by you in the grading queue.')
          : ui.h('div', {class: 'field'}, ui.h('span', {text: q.type === 'single' ? 'Options — mark the one correct answer' : 'Options — mark every correct answer'}), optionsEditor(q, redraw)));
    }

    function redraw() {
      fields.clear();
      body.replaceChildren(
        state.questions.length ? ui.h('div', {class: 'grid'}, state.questions.map((q, i) => questionBlock(q, i, redraw)))
          : ui.empty({icon: 'check', title: 'No questions yet', text: 'Add questions by hand, or draft them from the lessons with the server LLM.'}),
        ui.h('div', {class: 'actions'}, Object.entries(TYPES).map(([value, text]) => ui.h('button', {type: 'button', class: 'btn sm',
          disabled: state.questions.length >= LIMITS.questions, onclick: () => { state.questions.push(blank(value)); redraw(); }}, ui.icon('plus', 'sm'), text))));
    }

    function showErrors(errors) {
      for (const node of fields.values()) node.removeAttribute('aria-invalid');
      if (!errors.length) { alertBox.hidden = true; return; }
      for (const [key] of errors) if (key) fields.get(key)?.setAttribute('aria-invalid', 'true');
      alertBox.hidden = false;
      alertBox.replaceChildren(ui.h('strong', {text: 'Fix before saving:'}), ui.h('ul', {}, errors.slice(0, 8).map(([, text]) => ui.h('li', {text}))));
    }

    const draftButton = ui.h('button', {type: 'button', class: 'btn sm'}, ui.icon('spark', 'sm'), 'Draft with LLM');
    draftButton.addEventListener('click', async () => {
      if (state.questions.length && !await ui.confirm('Replace the questions with a draft?', 'Unsaved questions in the editor are replaced.', 'Replace', 'primary')) return;
      draftButton.disabled = true;
      try {
        const result = await post(ctx, 'quiz_draft', {module_id: state.module.id});
        state.questions = result.questions.map(fromQuestion);
        redraw();
        ui.toast(plural(result.questions.length, 'question', 'questions') + ' drafted' + (result.rejected.length ? ', ' + result.rejected.length + ' rejected (not quoted from a lesson)' : '') + '. Review, then save.', 'good');
      } catch (error) {
        ui.toast(error.message, 'bad');
      } finally {
        draftButton.disabled = false;
      }
    });
    const saveButton = ui.h('button', {type: 'button', class: 'btn primary sm'}, ui.icon('check', 'sm'), 'Save quiz');
    saveButton.addEventListener('click', async () => {
      const errors = validateQuiz(state.questions);
      showErrors(errors);
      if (errors.length) { ui.toast(errors[0][1], 'bad'); return; }
      saveButton.disabled = true;
      try {
        await post(ctx, 'quiz_set', {module_id: state.module.id, expected_revision: state.revision, questions: state.questions.map((q) => ({
          ...(q.id ? {id: q.id} : {}), type: q.type, prompt: q.prompt.trim(), points: Number(q.points),
          options: q.type === 'open' ? [] : q.options.map((o) => o.trim()), correct: q.type === 'open' ? [] : q.correct,
          rubric: q.type === 'open' ? q.rubric.trim() : '', lesson_id: q.lesson_id || null}))});
        ui.toast('Quiz saved', 'good');
        onSaved();
      } catch (error) {
        showErrors([[null, error.message]]);
        ui.toast(error.message, 'bad');
      } finally {
        saveButton.disabled = false;
      }
    });
    picker.addEventListener('change', load);
    load();
    return ui.card({title: 'Quiz', subtitle: 'One quiz per module. Employees see questions only, never the answer key.',
      actions: [picker, draftButton, saveButton]}, info, alertBox, body);
  }

  async function editors(ctx, ui, teamId, teamName, reload) {
    const data = await post(ctx, 'curriculum_get', {team_id: teamId});
    return [ui.h('h2', {class: 'tl-heading', text: 'Manage onboarding'}),
      curriculumEditor(ctx, ui, teamId, teamName, data, reload), quizEditor(ctx, ui, data, reload)];
  }

  // ---------- Company ----------

  async function companyView(ctx, ui, openTeam) {
    const data = await ctx.api(API + 'kpis/company');
    const total = data.departments.length;
    return [ui.h('div', {class: 'kpis'},
      ui.kpi({label: 'Company completion', value: percent(data.completion_percent), tone: 'accent', sub: 'weighted by department size'}),
      ui.kpi({label: 'Departments with a curriculum', value: data.with_curriculum + ' / ' + total}),
      ui.kpi({label: 'Finished onboarding', value: String(data.finished), sub: data.enrolled + ' enrolled'}),
      ui.kpi({label: 'Awaiting grading', value: String(data.pending_grading), tone: data.pending_grading ? 'warn' : ''})),
    ui.card({title: 'Departments', flush: true}, ui.table([
      {title: 'Department', render: (d) => ui.h('div', {class: 'person'}, ui.h('div', {}, ui.h('strong', {text: d.name}), ui.h('small', {text: d.team_id})))},
      {title: 'Curriculum', render: (d) => d.has_curriculum ? ui.chip(plural(d.modules, 'module', 'modules'), 'good', {dot: true}) : ui.chip('None', 'warn', {dot: true})},
      {title: 'Enrolled', numeric: true, render: (d) => ui.h('span', {class: 'num', text: d.enrolled + ' / ' + d.members})},
      {title: 'Completion', render: (d) => d.has_curriculum ? bar(ui, d.completion_percent, d.name + ' completion') : ui.h('span', {class: 'muted', text: '—'})},
      {title: 'Avg score', numeric: true, render: (d) => ui.h('span', {class: 'num', text: percent(d.average_score_percent)})},
      {title: 'To grade', numeric: true, render: (d) => d.pending_grading ? ui.chip(String(d.pending_grading), 'warn') : ui.h('span', {class: 'num muted', text: '0'})},
      {title: '', render: (d) => ui.h('button', {type: 'button', class: 'btn ghost sm', onclick: () => openTeam(d.team_id)}, 'Open')},
    ], data.departments, ui.empty({icon: 'building', title: 'No departments yet'})))];
  }

  // ---------- Mount ----------

  async function mount(container, ctx) {
    const ui = ctx.ui;
    const overview = await post(ctx, 'overview');
    const tabs = [];
    if (overview.teams.length || !overview.viewable_teams.length) tabs.push(['mine', 'My courses']);
    if (overview.viewable_teams.length) tabs.push(['team', 'Department']);
    if (OVERSIGHT.includes(overview.user.org_role)) tabs.push(['company', 'Company']);
    const body = ui.h('div', {class: 'grid'});
    const teamPicker = ui.h('select', {'aria-label': 'Department'}, overview.viewable_teams.map((t) => ui.h('option', {value: t.team_id, text: t.name})));
    const managed = overview.manageable_teams[0] || overview.viewable_teams[0];
    if (managed) teamPicker.value = managed.team_id;
    const tabBar = ui.h('div', {class: 'segmented tl-tabs', role: 'tablist', 'aria-label': 'Onboarding views'});
    let current = null;

    function select(name) {
      current = name;
      for (const button of tabBar.querySelectorAll('button')) {
        const on = button.dataset.tab === name;
        button.setAttribute('aria-selected', String(on));
        button.tabIndex = on ? 0 : -1;
      }
      teamPicker.hidden = name !== 'team' || overview.viewable_teams.length < 2;
      if (name === 'mine') ui.load(body, () => myCourses(ctx, ui, overview));
      if (name === 'team') ui.load(body, () => department(ctx, ui, overview, teamPicker.value, body));
      if (name === 'company') ui.load(body, () => companyView(ctx, ui, (teamId) => { teamPicker.value = teamId; select('team'); }));
    }

    for (const [name, label] of tabs) {
      tabBar.append(ui.h('button', {type: 'button', role: 'tab', 'data-tab': name, onclick: () => select(name)}, label));
    }
    tabBar.addEventListener('keydown', (event) => {
      if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
      const index = tabs.findIndex(([name]) => name === current);
      const next = tabs[(index + (event.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length][0];
      select(next);
      tabBar.querySelector('[data-tab="' + next + '"]').focus();
    });
    teamPicker.addEventListener('change', () => select('team'));
    tabBar.classList.add('tl-tabs-' + tabs.length);
    container.append(ui.h('div', {class: 'hero tl-hero'},
      ui.h('p', {text: overview.teams.length ? 'Study your department’s knowledge, take the quizzes and track results.'
        : 'Follow onboarding progress across departments.'}),
      ui.h('div', {class: 'actions'}, tabs.length > 1 ? tabBar : null, teamPicker)), body);
    const preferred = overview.manageable_teams.length ? 'team'
      : !overview.teams.length && OVERSIGHT.includes(overview.user.org_role) ? 'company' : tabs[0][0];
    select(tabs.some(([name]) => name === preferred) ? preferred : tabs[0][0]);
  }

  window.TamLearning = {mount};
})();
