'use strict';

const $ = (id) => document.getElementById(id);
const selected = new Set();
const ROLE_LABELS = {
  has: 'has · целое → часть',
  a_part_of: 'a_part_of · часть → целое',
  is_a: 'is_a · вид → класс',
  is_instance: 'is_instance · экземпляр → класс',
  used_for: 'used_for · назначение',
  use: 'use · использует',
  output: 'output · результат',
  input: 'input · вход',
  same_as: 'same_as · равнозначно (без обхода)',
  ignore: 'Не включать в учебный маршрут'
};
let allTopics = [];
let allOntologies = [];
let currentOntologyId = 'default';
let currentSession = null;
let isBusy = false;
let graphNodeId = null;
const SVG_NS = "http://www.w3.org/2000/svg";

function setError(message, id = 'sidebarError') { $(id).textContent = message || ''; }
function lockSettings(active) {
  const locked = Boolean(active);
  for (const id of ['questionCount', 'searchTopics', 'allTopics', 'clearTopics', 'ontologySelect', 'uploadButton', 'ontologyFile', 'saveRelations']) {
    $(id).disabled = locked;
  }
  $('startButton').disabled = locked;
  $('lockLabel').textContent = locked ? '🔒 Заблокированы' : 'Доступны';
  $('lockLabel').classList.toggle('locked', locked);
  document.querySelectorAll('.topic input[type="checkbox"], .relation-role').forEach(input => input.disabled = locked);
}
function updateSelectedCount() { $('selectedCount').textContent = `${selected.size} выбрано`; }
function drawTopics() {
  const list = $('topicList');
  list.replaceChildren();
  const query = $('searchTopics').value.toLowerCase().replaceAll('ё', 'е').trim();
  const shown = allTopics.filter(t => `${t.name} ${t.id}`.toLowerCase().replaceAll('ё', 'е').includes(query));
  for (const topic of shown) {
    const label = document.createElement('label'); label.className = 'topic';
    const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.value = topic.id;
    checkbox.checked = selected.has(topic.id);
    checkbox.disabled = Boolean(currentSession?.active);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selected.add(topic.id); else selected.delete(topic.id);
      updateSelectedCount();
    });
    const name = document.createElement('span'); name.textContent = topic.name;
    const extra = document.createElement('small'); extra.textContent = `#${topic.id} · ${topic.descendants}`;
    label.append(checkbox, name, extra); list.append(label);
  }
  if (!shown.length) {
    const p = document.createElement('p'); p.className = 'no-topics'; p.textContent = 'Ничего не найдено'; list.append(p);
  }
  updateSelectedCount();
  if (!currentSession?.active) $('graphButton').disabled = !selected.size;
}
async function callApi(url, method = 'GET', payload = null) {
  const options = {method, headers: {}};
  if (payload instanceof FormData) options.body = payload;
  else if (payload !== null) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(payload);
  }
  let response;
  try { response = await fetch(url, options); }
  catch (_) { throw Error('Нет связи с сервером. Проверьте, что FastAPI запущен.'); }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = data.detail;
    throw Error(typeof detail === 'string' ? detail : 'Не удалось выполнить действие. Проверьте параметры запроса.');
  }
  return data;
}
function setComposer(allow) {
  $('messageInput').disabled = !allow;
  $('sendButton').disabled = !allow;
  $('messageInput').placeholder = !allow ? 'Сначала начните учебную сессию…' : currentSession?.mode === 'choose_start' ? 'С чего хотите начать? Например: Что такое нейрон?' : `Спросите о понятии «${currentSession?.current?.name || 'текущая тема'}»…`;
}
function showMessage(role, content, result = null) {
  const empty = $('messages').querySelector('.empty-state'); if (empty) empty.remove();
  const outer = document.createElement('article');
  outer.className = `chat-message ${role === 'user' ? 'me' : 'bot'} ${['out_of_order', 'out_of_scope', 'unrecognized'].includes(result?.result) ? 'notice' : ''}`;
  const caption = document.createElement('span'); caption.className = 'message-caption';
  caption.textContent = role === 'user' ? 'Вы' : result?.result === 'out_of_order' ? 'Подсказка по порядку' : result?.result === 'out_of_scope' ? 'Тема вне выбранной области' : 'OntoTutor · онтология';
  const bubble = document.createElement('div'); bubble.className = 'message-bubble'; bubble.textContent = content;
  outer.append(caption, bubble);
  if (role === 'bot' && ['answered', 'aside'].includes(result?.result) && result?.answered_node?.id) {
    const graphLink = document.createElement('button');
    graphLink.type = 'button'; graphLink.className = 'graph-answer-link';
    graphLink.textContent = 'Посмотреть связи понятия';
    graphLink.addEventListener('click', () => openGraph(result.answered_node.id));
    outer.append(graphLink);
  }
  $('messages').append(outer);
  $('messages').scrollTop = $('messages').scrollHeight;
}
function addLi(list, value) { const item = document.createElement('li'); item.textContent = value; list.append(item); }
function displayState(state) {
  currentSession = state;
  const picking = state.mode === 'choose_start';
  lockSettings(state.active);
  $('sessionBadge').textContent = state.active ? '● Сессия активна' : state.reason === 'forced' ? 'Сессия прервана' : '✓ Сессия завершена';
  $('sessionBadge').classList.toggle('active', state.active);
  $('currentTopic').textContent = picking ? 'Выберите точку вопросом' : state.current.name;
  $('currentHint').textContent = picking ? (state.branches_completed ? 'Тема изучена · можно начать следующую' : 'Можно начать с любой выбранной темы или её подтемы') : 'Изучаем понятия по порядку; задайте вопрос о текущей теме.';
  $('topicIndex').textContent = picking ? (state.branches_completed ? '✓' : '—') : `${state.current_index + 1} / ${state.course_length}`;
  $('countProgress').textContent = `${state.questions_answered} / ${state.question_count}`;
  $('progressBar').style.width = `${Math.min(100, Math.round(state.questions_answered / state.question_count * 100))}%`;
  $('progressHint').textContent = state.active ? (picking ? `Доступно ${state.allowed_count} понятий · завершено веток: ${state.branches_completed}` : `Ветка ${state.branch_number}: ${state.branch_answered} из ${state.course_length} · осталось до минимума: ${state.remaining}`) : 'Можно начать новую сессию';
  $('finishButton').disabled = !state.can_finish;
  $('forceButton').disabled = !state.active;
  $('composerStatus').textContent = !state.active ? 'Настройки доступны' : picking ? 'Выберите понятие вопросом' : `Тема ${state.branch_number} · шаг ${state.current_index + 1}/${state.course_length}`;
  $('chatMode').textContent = picking ? 'Режим: выбор темы' : 'Режим: изучение темы';
  $('statusMode').textContent = !state.active ? 'Завершено' : picking ? 'Выбор темы' : 'Изучение';
  $('statusTopic').textContent = picking ? 'Выберите понятие вопросом' : state.current.name;
  $('statusStep').textContent = state.active ? `${state.questions_answered} / ${state.question_count}` : `${state.questions_answered} завершено`;
  $('statusAction').textContent = !state.active ? 'Можно изменить настройки или начать новое занятие' : picking
    ? 'Напишите первый вопрос' : `Сейчас: «${state.current.name}». Задайте вопрос`;
  $('graphButton').disabled = !(state.current?.id || selected.size);
  const path = $('lessonPath'); path.replaceChildren();
  if (picking) {
    const chip = document.createElement('span'); chip.className = 'path-chip';
    chip.textContent = state.branches_completed ? '✓ Можно выбрать новую ветку' : 'Первый вопрос определяет маршрут';
    path.append(chip);
  } else {
    for (const item of state.trace.slice(-2)) {
      const chip = document.createElement('span'); chip.className = 'path-chip'; chip.textContent = item.name; path.append(chip);
    }
    if (state.next.length) {
      const chip = document.createElement('span'); chip.className = 'path-chip next-chip';
      chip.textContent = `Далее: ${state.next[0].name}`; path.append(chip);
    }
  }
  const suggestions = $('suggestions'); suggestions.replaceChildren();
  if (state.active) {
    for (const hint of state.suggestions) {
      const b = document.createElement('button'); b.type = 'button'; b.className = 'suggestion'; b.textContent = hint;
      b.addEventListener('click', () => { $('messageInput').value = hint; $('messageInput').focus(); });
      suggestions.append(b);
    }
  }
  setComposer(state.active && !isBusy);
}
function resetChat() {
  currentSession = null;
  $('sessionBadge').textContent = 'Сессия не начата';
  $('sessionBadge').classList.remove('active');
  $('currentTopic').textContent = 'Выберите тему слева';
  $('currentHint').textContent = 'Первый вопрос выберет начало ветки';
  $('topicIndex').textContent = '—';
  $('countProgress').textContent = `0 / ${$('questionCount').value || 2}`;
  $('progressBar').style.width = '0%';
  $('progressHint').textContent = 'Начните с выбранных тем';
  $('lessonPath').replaceChildren(); $('suggestions').replaceChildren();
  $('chatMode').textContent = 'Режим: выбор темы';
  $('statusMode').textContent = 'Настройка';
  $('statusTopic').textContent = allOntologies.find(x => x.id === currentOntologyId)?.title || 'Не выбрана';
  $('statusStep').textContent = '—';
  $('statusAction').textContent = 'Выберите темы и нажмите «Начать обучение»';
  $('graphButton').disabled = !selected.size;
  $('finishButton').disabled = true; $('forceButton').disabled = true;
  $('composerStatus').textContent = 'Настройки доступны до старта';
  $('messages').replaceChildren();
  showMessage('bot', 'Добро пожаловать в OntoTutor! Выберите область знаний и темы слева, затем начните обучение. Можно спрашивать своими словами и открывать граф связей. Кнопка «ⓘ Справка» расскажет, что делать на каждом шаге.');
  setComposer(false);
  lockSettings(false);
}
function drawOntologyList() {
  const select = $('ontologySelect'); select.replaceChildren();
  for (const item of allOntologies) {
    const option = document.createElement('option'); option.value = item.id;
    option.textContent = `${item.title} · ${item.nodes} пон.`;
    select.append(option);
  }
  select.value = currentOntologyId;
}
async function refreshOntologyList(preferredId = null) {
  const data = await callApi('/api/ontologies');
  allOntologies = data.items;
  const ids = allOntologies.map(x => x.id);
  if (preferredId && ids.includes(preferredId)) currentOntologyId = preferredId;
  else if (!ids.includes(currentOntologyId)) currentOntologyId = data.default_id;
  drawOntologyList();
}
function renderRelations(data) {
  const root = $('relationMap'); root.replaceChildren();
  let ignored = 0;
  let unnamed = 0;
  for (const item of data.types) {
    const label = document.createElement('label'); label.className = 'relation-entry';
    const text = document.createElement('span'); text.textContent = `${item.name || '(без имени)'} · ${item.count}`;
    text.title = item.name || '(без имени)';
    const select = document.createElement('select'); select.className = 'relation-role';
    select.dataset.relationName = item.name;
    for (const role of data.roles) {
      const option = document.createElement('option'); option.value = role;
      option.textContent = ROLE_LABELS[role] || role;
      select.append(option);
    }
    select.value = item.role;
    select.disabled = Boolean(currentSession?.active);
    label.append(text, select); root.append(label);
    if (item.role === 'ignore') {
      ignored += item.count;
      if (!item.name.trim()) unnamed += item.count;
    }
  }
  const labels = [];
  if (unnamed) labels.push(`${unnamed} без названия`);
  if (ignored > unnamed) labels.push(`${ignored - unnamed} без маршрута`);
  $('relationWarning').textContent = labels.length ? `· ${labels.join(', ')}` : '';
  $('relationSettings').hidden = data.types.length === 0;
}
async function loadGraph(id, {preserveSelected = false} = {}) {
  const [meta, relations] = await Promise.all([
    callApi(`/api/meta?ontology_id=${encodeURIComponent(id)}`),
    callApi(`/api/ontologies/${encodeURIComponent(id)}/relations`)
  ]);
  currentOntologyId = id;
  $('ontologySelect').value = id;
  localStorage.setItem('ontotutor_ontology_id', id);
  allTopics = meta.topics;
  if (!preserveSelected) {
    selected.clear(); meta.default_selected.forEach(nid => selected.add(nid));
  } else {
    const valid = new Set(meta.topics.map(t => t.id));
    for (const nid of selected) if (!valid.has(nid)) selected.delete(nid);
  }
  $('graphStats').textContent = `${meta.nodes} понятий · ${meta.relations} связей`;
  const missing = meta.nodes - meta.definitions;
  $('ontologyInfo').textContent = missing ? `Определения: ${meta.definitions} из ${meta.nodes}. Для ${missing} понятий нет определения.` : `Определения заполнены: ${meta.definitions} из ${meta.nodes}.`;
  renderRelations(relations);
  drawTopics();
}
async function switchOntology(id) {
  if (currentSession?.active) return;
  setError('', 'ontologyError'); setError(''); setError('', 'chatError');
  $('ontologySelect').disabled = true;
  try {
    await loadGraph(id);
    $('searchTopics').value = '';
    resetChat(); drawTopics();
  } catch (error) {
    setError(error.message, 'ontologyError');
    $('ontologySelect').value = currentOntologyId;
  } finally { $('ontologySelect').disabled = Boolean(currentSession?.active); }
}
async function uploadOntology() {
  if (currentSession?.active) return;
  const file = $('ontologyFile').files?.[0]; if (!file) return;
  setError('', 'ontologyError');
  if (file.size > 5 * 1024 * 1024) {
    setError('Максимальный размер файла — 5 МБ.', 'ontologyError'); $('ontologyFile').value = ''; return;
  }
  const fd = new FormData(); fd.append('file', file);
  $('uploadButton').disabled = true;
  $('uploadButton').textContent = 'Загружаем…';
  try {
    const created = await callApi('/api/ontologies', 'POST', fd);
    await refreshOntologyList(created.id);
    await switchOntology(created.id);
    showMessage('bot', `Добавлена онтология «${created.title}»: ${created.nodes} понятий и ${created.relations} связей. Выберите темы слева. Если есть нестандартные связи, раскройте «Настроить типы связей».`);
    if (created.unmapped_relations) $('relationSettings').open = true;
  } catch (error) { setError(error.message, 'ontologyError'); }
  finally {
    $('ontologyFile').value = '';
    $('uploadButton').textContent = '＋ Загрузить .ont';
    $('uploadButton').disabled = Boolean(currentSession?.active);
  }
}
async function saveRelationMapping() {
  if (currentSession?.active) return;
  setError('', 'ontologyError');
  const mapping = {};
  for (const control of document.querySelectorAll('.relation-role')) mapping[control.dataset.relationName] = control.value;
  $('saveRelations').disabled = true;
  try {
    await callApi(`/api/ontologies/${encodeURIComponent(currentOntologyId)}/relations`, 'PUT', {mapping});
    await loadGraph(currentOntologyId, {preserveSelected: true});
    await refreshOntologyList(currentOntologyId);
    $('ontologyInfo').textContent += ' Роли связей сохранены.';
  } catch (error) { setError(error.message, 'ontologyError'); }
  finally { $('saveRelations').disabled = Boolean(currentSession?.active); }
}
async function start() {
  setError(''); setError('', 'chatError');
  if (!selected.size) { setError('Выберите хотя бы одно понятие.'); return; }
  const count = Number($('questionCount').value);
  if (!Number.isInteger(count) || count < 1 || count > 100) {
    setError('Количество вопросов должно быть целым числом от 1 до 100.'); return;
  }
  try {
    $('startButton').disabled = true;
    const data = await callApi('/api/sessions', 'POST', {
      ontology_id: currentOntologyId, selected_topics: [...selected], question_count: count
    });
    localStorage.setItem('ontotutor_session_id', data.session_id);
    $('messages').replaceChildren(); displayState(data);
    showMessage('bot', `Начнём изучение. Задайте первый вопрос по одной из выбранных тем. Я объясню понятие и покажу связанные с ним темы. Для завершения занятия нужно задать не менее ${count} вопросов. Если потребуется помощь, нажмите «Справка».`);
    $('messageInput').focus();
  } catch (error) { setError(error.message); $('startButton').disabled = false; }
}
async function send() {
  if (!currentSession?.active || isBusy) return;
  const question = $('messageInput').value.trim();
  if (!question) { setError('Введите вопрос.', 'chatError'); return; }
  setError('', 'chatError'); isBusy = true; setComposer(false);
  $('statusAction').textContent = 'Обрабатываем вопрос и проверяем связи…';
  try {
    const data = await callApi(`/api/sessions/${currentSession.session_id}/ask`, 'POST', {question});
    showMessage('user', question); showMessage('bot', data.answer, data);
    $('messageInput').value = ''; displayState(data.state);
    if (data.result === 'answered' && data.branch_completed) {
      showMessage('bot', 'Ветка полностью пройдена! Теперь можно задать новый вопрос по любому понятию из выбранной области — и начать следующую ветку.');
    }
    if (data.result === 'answered' && data.state.can_finish && data.state.questions_answered === data.state.question_count) {
      showMessage('bot', 'Минимальное количество вопросов выполнено. Можно завершить сессию или продолжить обучение.');
    }
  } catch (error) {
    setError(error.message, 'chatError');
    $('statusAction').textContent = 'Ошибка: исправьте вопрос или проверьте соединение';
  }
  finally { isBusy = false; setComposer(Boolean(currentSession?.active)); if (currentSession?.active) $('messageInput').focus(); }
}
async function closeSession(force) {
  if (!currentSession?.active) return;
  if (force && !confirm('Прервать учебную сессию? Незаданные вопросы не будут засчитаны.')) return;
  try {
    const data = await callApi(`/api/sessions/${currentSession.session_id}/${force ? 'force' : 'complete'}`, 'POST');
    localStorage.removeItem('ontotutor_session_id');
    displayState(data);
    showMessage('bot', force ? 'Сессия прервана. Можно сменить предметную область и запустить новую.' : 'Сессия завершена. Вы можете изменить настройки или выбрать другую онтологию.');
    drawTopics();
  } catch (error) { setError(error.message, 'chatError'); }
}
async function initialize() {
  try {
    const remembered = localStorage.getItem('ontotutor_ontology_id');
    await refreshOntologyList(remembered);
    const sid = localStorage.getItem('ontotutor_session_id');
    let restored = null;
    if (sid) {
      try {
        const state = await callApi(`/api/sessions/${encodeURIComponent(sid)}`);
        if (state.active && allOntologies.some(item => item.id === state.ontology_id)) restored = state;
        else localStorage.removeItem('ontotutor_session_id');
      } catch (_) { localStorage.removeItem('ontotutor_session_id'); }
    }
    await loadGraph(restored ? restored.ontology_id : currentOntologyId);
    resetChat();
    if (restored) {
      selected.clear(); restored.selected_topics.forEach(nid => selected.add(nid));
      $('questionCount').value = restored.question_count;
      drawTopics(); displayState(restored);
      $('messages').replaceChildren();
      const history = await callApi(`/api/sessions/${encodeURIComponent(restored.session_id)}/history`);
      for (const item of history.items) {
        showMessage(item.role, item.content, item.role === 'bot' ? item : null);
      }
      showMessage('bot', `Продолжаем занятие «${restored.ontology_title}». ${restored.mode === 'choose_start' ? 'Можно выбрать тему новым вопросом.' : `Текущее понятие — «${restored.current.name}».`} Прогресс: ${restored.questions_answered} из ${restored.question_count}.`);
    }
  } catch (error) { setError(error.message, 'ontologyError'); }
  // Показываем инструкцию один раз; её всегда можно открыть повторно в шапке.
  if (!localStorage.getItem('ontotutor_help_seen')) {
    localStorage.setItem('ontotutor_help_seen', '1');
    $('helpDialog').showModal();
  }
}

function svgElement(tag, attributes = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [key, val] of Object.entries(attributes)) el.setAttribute(key, String(val));
  return el;
}
function fitGraphLabel(text) {
  const name = text.length > 37 ? `${text.slice(0, 35)}…` : text;
  const words = name.split(' '), lines = [];
  let line = '';
  for (const word of words) {
    if (line && `${line} ${word}`.length > 19) { lines.push(line); line = word; }
    else line = line ? `${line} ${word}` : word;
  }
  if (line) lines.push(line);
  return lines.slice(0, 2).map((part, index) => index === 1 && lines.length > 2 ? `${part.slice(0, 17)}…` : part);
}
function ellipseEndpoint(from, to, rx, ry, extra = 0) {
  const dx = to[0] - from[0], dy = to[1] - from[1];
  const norm = Math.sqrt(dx * dx / (rx * rx) + dy * dy / (ry * ry)) || 1;
  const distance = Math.hypot(dx, dy) || 1;
  return [from[0] + dx / norm + extra * dx / distance,
          from[1] + dy / norm + extra * dy / distance];
}
/* ───────── Интерактивный граф ─────────
   Радиальная раскладка + разведение овалов + обход узлов рёбрами.
   Показываются ВСЕ связи между отображаемыми понятиями. */
const EDGE_COLORS = {
  is_a: '#7a5cf5', is_instance: '#9b6df0', a_part_of: '#16a085', has: '#2aa889',
  use: '#e08a3c', used_for: '#e08a3c', use_for: '#e08a3c', input: '#d9637a', output: '#d9637a', same_as: '#5a8def'
};
const EDGE_FALLBACK = '#8a8aa6';
function edgeColor(type) { return EDGE_COLORS[(type || '').toLowerCase()] || EDGE_FALLBACK; }
function edgeKey(type) { return (type || 'none').toLowerCase().replace(/[^a-z0-9а-яё]+/gi, '_'); }

function nodeSize(name, center) {
  const lines = fitGraphLabel(name);
  const longest = Math.max(...lines.map(l => l.length), 4);
  const rx = Math.max(center ? 78 : 58, longest * 4.0 + (center ? 30 : 24));
  const ry = (lines.length > 1 ? 28 : 22) + (center ? 5 : 0);
  return {lines, rx, ry};
}
function ellipseHit(cx, cy, rx, ry, x, y, pad = 0) {
  const dx = (x - cx) / (rx + pad), dy = (y - cy) / (ry + pad);
  return dx * dx + dy * dy < 1;
}
function layoutGraph(graph) {
  const centerId = graph.center_id;
  const nodes = graph.nodes.map(n => ({...n, size: nodeSize(n.name, n.id === centerId)}));
  const byId = new Map(nodes.map(n => [n.id, n]));
  const links = graph.edges.filter(e => byId.has(e.source_id) && byId.has(e.destination_id));
  const adj = new Map(nodes.map(n => [n.id, new Set()]));
  links.forEach(e => { adj.get(e.source_id).add(e.destination_id); adj.get(e.destination_id).add(e.source_id); });
  const center = byId.get(centerId);
  center.x = 0; center.y = 0;
  const ring1 = nodes.filter(n => n.id !== centerId && (n.level ?? 1) <= 1);
  const ring2 = nodes.filter(n => n.id !== centerId && (n.level ?? 1) >= 2);
  const ASPECT = 1.5;                         // схема шире, чем выше, как и окно

  // 1. Каждому узлу 2-го уровня — главный родитель с 1-го (самый «густой» хаб).
  const children = new Map(ring1.map(n => [n.id, []]));
  const orphans = [];
  ring2.forEach(n => {
    const parent = [...adj.get(n.id)].map(id => byId.get(id)).filter(p => p && children.has(p.id))
      .sort((p, q) => adj.get(q.id).size - adj.get(p.id).size)[0];
    if (parent) children.get(parent.id).push(n); else orphans.push(n);
  });

  // 2. Сектор каждого узла 1-го уровня пропорционален числу его потомков; самый «тяжёлый» — вправо.
  const weight = n => 1 + (children.get(n.id)?.length || 0) * 0.75;
  ring1.sort((p, q) => weight(q) - weight(p));
  const order = [];
  ring1.forEach((n, i) => (i % 2 ? order.push(n) : order.unshift(n)));
  const total = order.reduce((sum, n) => sum + weight(n), 0) || 1;
  const R1 = Math.max(185, 44 + ring1.length * 20);
  const heavyIndex = order.indexOf(ring1[0]);
  let cursor = 0; const centers = [];
  order.forEach(n => { const span = 2 * Math.PI * weight(n) / total; centers.push({n, a: cursor + span / 2, span}); cursor += span; });
  const shift = heavyIndex >= 0 ? -centers[heavyIndex].a : 0;      // тяжёлый сектор → угол 0 (вправо)
  centers.forEach(({n, a, span}) => {
    n.angle = a + shift; n.span = span;
    n.x = Math.cos(n.angle) * R1 * ASPECT; n.y = Math.sin(n.angle) * R1;
  });

  // 3. Потомки — веером в несколько дуг за родителем.
  centers.forEach(({n: parent, span}) => {
    const list = children.get(parent.id) || [];
    if (!list.length) return;
    const fan = Math.min(span * 0.92, Math.PI * 0.95);
    let placed = 0, row = 0;
    while (placed < list.length) {
      const r = R1 + 150 + row * 100;
      const capacity = Math.max(2, Math.floor(fan * r * 1.3 / 145));
      const take = Math.min(capacity, list.length - placed);
      for (let i = 0; i < take; i++) {
        const t = take === 1 ? 0 : (i / (take - 1) - 0.5);
        const a = parent.angle + t * fan * (take === 1 ? 0 : 1) + (row % 2 ? (fan / Math.max(take, 2)) * 0.25 : 0);
        const child = list[placed + i];
        child.angle = a; child.x = Math.cos(a) * r * ASPECT; child.y = Math.sin(a) * r;
      }
      placed += take; row++;
    }
  });
  orphans.forEach((n, i) => {
    const a = Math.PI + i * 0.6; n.angle = a;
    n.x = Math.cos(a) * (R1 + 220) * ASPECT; n.y = Math.sin(a) * (R1 + 220);
  });

  // 4. Только разведение пересечений: положение узлов дерева почти не меняется.
  for (let step = 0; step < 260; step++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const a = nodes[i], b = nodes[j];
        let dx = b.x - a.x, dy = b.y - a.y;
        if (!dx && !dy) { dx = 0.01 * (i + 1); dy = 0.01; }
        const gapX = a.size.rx + b.size.rx + 34, gapY = a.size.ry + b.size.ry + 28;
        const ox = gapX - Math.abs(dx), oy = gapY - Math.abs(dy);
        if (ox <= 0 || oy <= 0) continue;
        moved = true;
        const fx = ox / gapX < oy / gapY ? Math.sign(dx) * ox * 0.5 : 0;
        const fy = fx ? 0 : Math.sign(dy) * oy * 0.5;
        if (a.id !== centerId) { a.x -= fx; a.y -= fy; }
        if (b.id !== centerId) { b.x += fx; b.y += fy; }
      }
    }
    if (!moved) break;
  }
  return {nodes, byId, links};
}
function routeEdges(layout) {
  // Прямая линия, а если она проходит через чужой овал — плавный изгиб в свободную сторону.
  const {nodes, byId, links} = layout;
  const pairCount = new Map();
  return links.map((e, index) => {
    const a = byId.get(e.source_id), b = byId.get(e.destination_id);
    const pairKey = [e.source_id, e.destination_id].sort().join('|');
    const sibling = pairCount.get(pairKey) || 0; pairCount.set(pairKey, sibling + 1);
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    const nx = -dy / len, ny = dx / len;
    const blocked = (off) => {
      const cx = mx + nx * off, cy = my + ny * off;
      for (let t = 0.12; t <= 0.88; t += 0.04) {
        const x = (1 - t) * (1 - t) * a.x + 2 * (1 - t) * t * cx + t * t * b.x;
        const y = (1 - t) * (1 - t) * a.y + 2 * (1 - t) * t * cy + t * t * b.y;
        for (const n of nodes) {
          if (n === a || n === b) continue;
          if (ellipseHit(n.x, n.y, n.size.rx, n.size.ry, x, y, 6)) return true;
        }
      }
      return false;
    };
    let offset = sibling ? 34 * (sibling % 2 ? 1 : -1) : 0;
    if (blocked(offset)) {
      const tries = [40, -40, 80, -80, 120, -120, 170, -170];
      const free = tries.find(v => !blocked(v));
      offset = free ?? tries[0];
    }
    const control = [mx + nx * offset * 2, my + ny * offset * 2];
    // Концы — на границах овалов (по направлению к контрольной точке).
    const start = ellipseEndpoint([a.x, a.y], control, a.size.rx, a.size.ry, 2);
    const end = ellipseEndpoint([b.x, b.y], control, b.size.rx, b.size.ry, 7);
    return {edge: e, a, b, start, end, control};
  });
}
function pointOnCurve(r, t) {
  const [sx, sy] = r.start, [ex, ey] = r.end, [cx, cy] = r.control;
  return [(1 - t) * (1 - t) * sx + 2 * (1 - t) * t * cx + t * t * ex,
          (1 - t) * (1 - t) * sy + 2 * (1 - t) * t * cy + t * t * ey];
}
function placeLabels(routes, nodes) {
  // Подписи ставим вдоль ребра так, чтобы они не перекрывали друг друга и овалы.
  const placed = [];
  const overlaps = (x, y, w) => {
    const box = {x1: x - w / 2 - 3, x2: x + w / 2 + 3, y1: y - 12, y2: y + 12};
    if (placed.some(p => box.x1 < p.x2 && box.x2 > p.x1 && box.y1 < p.y2 && box.y2 > p.y1)) return true;
    return nodes.some(n => box.x2 > n.x - n.size.rx && box.x1 < n.x + n.size.rx && box.y2 > n.y - n.size.ry && box.y1 < n.y + n.size.ry);
  };
  routes.forEach(r => {
    const text = r.edge.type && r.edge.type !== '(без типа)' ? r.edge.type : 'без типа';
    const shown = text.length > 19 ? `${text.slice(0, 18)}…` : text;
    const w = Math.max(34, shown.length * 7 + 16);
    let best = null;
    for (const t of [0.5, 0.4, 0.6, 0.32, 0.68, 0.25, 0.75, 0.18, 0.82]) {
      const [x, y] = pointOnCurve(r, t);
      if (!overlaps(x, y, w)) { best = [x, y]; break; }
    }
    if (!best) best = pointOnCurve(r, 0.5);
    placed.push({x1: best[0] - w / 2 - 3, x2: best[0] + w / 2 + 3, y1: best[1] - 12, y2: best[1] + 12});
    r.label = {x: best[0], y: best[1], w, text: shown};
  });
}
function highlightNode(svg, nodeId) {
  // Наведение: показываем только связи выбранного овала, остальное приглушаем.
  const connected = new Set([nodeId]);
  svg.querySelectorAll('.graph-edge').forEach(el => {
    const on = el.dataset.from === nodeId || el.dataset.to === nodeId;
    if (on) { connected.add(el.dataset.from); connected.add(el.dataset.to); }
  });
  svg.classList.add('hovering');
  svg.querySelectorAll('.graph-edge, .graph-edge-label').forEach(el => el.classList.toggle('related', el.dataset.from === nodeId || el.dataset.to === nodeId));
  svg.querySelectorAll('.graph-node').forEach(el => el.classList.toggle('related', connected.has(el.dataset.nodeId)));
}
function clearHighlight(svg) {
  svg.classList.remove('hovering');
  svg.querySelectorAll('.related').forEach(el => el.classList.remove('related'));
}
function showGraphDefinition(node, graph) {
  const root = $('graphDetail'); root.replaceChildren();
  const kicker = document.createElement('span'); kicker.className = 'detail-kicker';
  kicker.textContent = node.id === graph.center_id ? 'ЦЕНТРАЛЬНОЕ ПОНЯТИЕ' : 'ВЫБРАННОЕ ПОНЯТИЕ';
  const header = document.createElement('h3'); header.textContent = node.name;
  const text = document.createElement('p');
  const raw = (node.definition || '').trim();
  if (!raw) text.textContent = 'Для этого понятия определение пока не добавлено.';
  else if (raw.toLocaleLowerCase('ru').startsWith(node.name.toLocaleLowerCase('ru'))) text.textContent = raw;
  else text.textContent = `${node.name} — ${raw.replace(/^[Ээ]то\s+/, 'это ')}`;
  root.append(kicker, header, text);
  // Список связей выбранного понятия — прямо в панели, чтобы можно было прочитать, не вглядываясь в схему.
  const mine = graph.edges.filter(e => e.source_id === node.id || e.destination_id === node.id);
  if (mine.length) {
    const title = document.createElement('div'); title.className = 'detail-rel-title'; title.textContent = `Связи · ${mine.length}`;
    const list = document.createElement('ul'); list.className = 'detail-rels';
    mine.forEach(e => {
      const item = document.createElement('li');
      const out = e.source_id === node.id;
      const dot = document.createElement('i'); dot.style.background = edgeColor(e.type);
      const label = document.createElement('b'); label.textContent = e.type || 'без типа';
      const other = document.createElement('span'); other.textContent = `${out ? '→' : '←'} ${out ? e.destination : e.source}`;
      item.append(dot, label, other); list.append(item);
    });
    root.append(title, list);
  }
  if (node.id !== graph.center_id) {
    const navigate = document.createElement('button');
    navigate.type = 'button'; navigate.className = 'graph-recenter';
    navigate.textContent = 'Показать связи этого понятия';
    navigate.addEventListener('click', () => openGraph(node.id));
    root.append(navigate);
  }
  $('graphSvg').querySelectorAll('.graph-node').forEach(el => el.classList.toggle('active', el.dataset.nodeId === node.id));
  $('graphSvg').querySelectorAll('.graph-edge').forEach(el => el.classList.toggle('active', el.dataset.from === node.id || el.dataset.to === node.id));
}
const graphView = {
  home: null, vb: null,
  apply() { const v = this.vb; $('graphSvg').setAttribute('viewBox', `${v.x.toFixed(1)} ${v.y.toFixed(1)} ${v.w.toFixed(1)} ${v.h.toFixed(1)}`); },
  reset() { this.vb = {...this.home}; this.apply(); },
  zoom(factor, cx = null, cy = null) {
    const v = this.vb; const nw = Math.min(Math.max(v.w / factor, this.home.w / 6), this.home.w * 2.2);
    const k = nw / v.w; const px = cx ?? v.x + v.w / 2, py = cy ?? v.y + v.h / 2;
    this.vb = {x: px - (px - v.x) * k, y: py - (py - v.y) * k, w: nw, h: v.h * k}; this.apply();
  }
};
function initGraphControls() {
  const svg = $('graphSvg');
  const toUnits = (e) => { const p = svg.createSVGPoint(); p.x = e.clientX; p.y = e.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); };
  svg.addEventListener('wheel', (e) => { e.preventDefault(); const p = toUnits(e); graphView.zoom(e.deltaY < 0 ? 1.15 : 1 / 1.15, p.x, p.y); }, {passive: false});
  let drag = null;
  svg.addEventListener('pointerdown', (e) => {
    if (e.target.closest('.graph-node') || e.button !== 0) return;
    drag = {x: e.clientX, y: e.clientY, vb: {...graphView.vb}, scale: svg.getScreenCTM().a};
    svg.setPointerCapture(e.pointerId); svg.classList.add('panning');
  });
  svg.addEventListener('pointermove', (e) => {
    if (!drag) return;
    graphView.vb = {...drag.vb, x: drag.vb.x - (e.clientX - drag.x) / drag.scale, y: drag.vb.y - (e.clientY - drag.y) / drag.scale};
    graphView.apply();
  });
  const stop = () => { drag = null; svg.classList.remove('panning'); };
  svg.addEventListener('pointerup', stop); svg.addEventListener('pointercancel', stop);
  $('graphZoomIn').addEventListener('click', () => graphView.zoom(1.3));
  $('graphZoomOut').addEventListener('click', () => graphView.zoom(1 / 1.3));
  $('graphFit').addEventListener('click', () => graphView.reset());
}
initGraphControls();
function renderGraph(graph) {
  const svg = $('graphSvg'); svg.replaceChildren(); svg.classList.remove('hovering');
  const central = graph.nodes.find(n => n.id === graph.center_id);
  $('graphTitle').textContent = `Связи: ${central?.name || 'понятие'}`;
  $('graphSummary').textContent = `${graph.nodes.length} понятий · ${graph.edges.length} связей${graph.truncated ? ' · показана часть графа' : ''}. Наведите на овал, чтобы выделить его связи; нажмите — прочитать определение.`;

  const layout = layoutGraph(graph);
  const routes = routeEdges(layout);
  placeLabels(routes, layout.nodes);

  // Границы схемы → viewBox по содержимому.
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  layout.nodes.forEach(n => {
    minX = Math.min(minX, n.x - n.size.rx); maxX = Math.max(maxX, n.x + n.size.rx);
    minY = Math.min(minY, n.y - n.size.ry); maxY = Math.max(maxY, n.y + n.size.ry);
  });
  routes.forEach(r => { minX = Math.min(minX, r.label.x - r.label.w / 2); maxX = Math.max(maxX, r.label.x + r.label.w / 2);
                        minY = Math.min(minY, r.label.y - 12); maxY = Math.max(maxY, r.label.y + 12); });
  const pad = 30;
  graphView.home = {x: minX - pad, y: minY - pad, w: maxX - minX + pad * 2, h: maxY - minY + pad * 2};
  graphView.reset();

  // Стрелки: по одной на цвет типа связи.
  const defs = svgElement('defs');
  const types = [...new Set(graph.edges.map(e => e.type || '(без типа)'))];
  types.forEach(type => {
    const marker = svgElement('marker', {id: `arrow-${edgeKey(type)}`, viewBox: '0 0 10 10', refX: '7.5', refY: '5', markerWidth: '6.5', markerHeight: '6.5', orient: 'auto-start-reverse'});
    marker.append(svgElement('path', {d: 'M0.5 0.8 L9.2 5 L0.5 9.2 Z', fill: edgeColor(type)}));
    defs.append(marker);
  });
  const gradient = svgElement('linearGradient', {id: 'graph-center-fill', x1: '0', y1: '0', x2: '1', y2: '1'});
  gradient.append(svgElement('stop', {offset: '0', 'stop-color': '#6a5ce0'}), svgElement('stop', {offset: '1', 'stop-color': '#3a2f96'}));
  defs.append(gradient); svg.append(defs);

  // Подписи легенды по реально встречающимся типам связей.
  const legend = $('graphLegend'); legend.replaceChildren();
  types.forEach(type => {
    const item = document.createElement('span'); item.className = 'legend-item';
    const dot = document.createElement('i'); dot.style.background = edgeColor(type);
    item.append(dot, document.createTextNode(type)); legend.append(item);
  });

  const edgeLayer = svgElement('g', {class: 'edge-layer'}), labelLayer = svgElement('g', {class: 'label-layer'}), nodeLayer = svgElement('g', {class: 'node-layer'});
  routes.forEach(r => {
    const color = edgeColor(r.edge.type), type = r.edge.type || '(без типа)';
    const path = svgElement('path', {
      d: `M${r.start[0].toFixed(1)} ${r.start[1].toFixed(1)} Q${r.control[0].toFixed(1)} ${r.control[1].toFixed(1)} ${r.end[0].toFixed(1)} ${r.end[1].toFixed(1)}`,
      class: 'graph-edge', stroke: color, 'marker-end': `url(#arrow-${edgeKey(type)})`, 'data-from': r.edge.source_id, 'data-to': r.edge.destination_id});
    const title = svgElement('title'); title.textContent = `${r.edge.source} — ${type} → ${r.edge.destination}`;
    path.append(title); edgeLayer.append(path);
    const label = svgElement('g', {class: 'graph-edge-label', 'data-from': r.edge.source_id, 'data-to': r.edge.destination_id});
    label.append(svgElement('rect', {x: (r.label.x - r.label.w / 2).toFixed(1), y: (r.label.y - 11).toFixed(1), width: r.label.w.toFixed(1), height: 22, rx: 11, stroke: color}));
    const labelText = svgElement('text', {x: r.label.x.toFixed(1), y: (r.label.y + 4).toFixed(1), fill: color}); labelText.textContent = r.label.text;
    label.append(labelText); labelLayer.append(label);
  });
  layout.nodes.forEach(node => {
    const center = node.id === graph.center_id;
    const group = svgElement('g', {class: `graph-node${center ? ' center' : ''}${node.level >= 2 ? ' outer' : ''}`,
      transform: `translate(${node.x.toFixed(1)} ${node.y.toFixed(1)})`, tabindex: '0', role: 'button',
      'data-node-id': node.id, 'aria-label': `Открыть определение: ${node.name}`});
    group.append(svgElement('ellipse', {rx: node.size.rx.toFixed(1), ry: node.size.ry.toFixed(1), ...(center ? {fill: 'url(#graph-center-fill)'} : {})}));
    const label = svgElement('text', {y: node.size.lines.length === 1 ? '5' : '-3'});
    node.size.lines.forEach((line, index) => {
      const span = svgElement('tspan', {x: '0', dy: index === 0 ? '0' : '16'});
      span.textContent = line; label.append(span);
    });
    group.append(label);
    const title = svgElement('title'); title.textContent = node.name; group.append(title);
    const select = () => showGraphDefinition(node, graph);
    group.addEventListener('click', select);
    group.addEventListener('mouseenter', () => highlightNode(svg, node.id));
    group.addEventListener('mouseleave', () => clearHighlight(svg));
    group.addEventListener('focus', () => highlightNode(svg, node.id));
    group.addEventListener('blur', () => clearHighlight(svg));
    group.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(); } });
    nodeLayer.append(group);
  });
  svg.append(edgeLayer, nodeLayer, labelLayer);   // подписи поверх овалов — всегда читаемы
  if (central) showGraphDefinition(central, graph);
}
async function openGraph(nodeId = null) {
  const first = [...selected][0] || allTopics[0]?.id;
  graphNodeId = nodeId || currentSession?.current?.id || first;
  if (!graphNodeId) {
    setError('Сначала выберите хотя бы одно понятие.', 'chatError'); return;
  }
  try {
    const data = await callApi(`/api/ontologies/${encodeURIComponent(currentOntologyId)}/graph/${encodeURIComponent(graphNodeId)}?depth=${$('graphDepth').value}`);
    renderGraph(data);
    if (!$('graphDialog').open) $('graphDialog').showModal();
  } catch (error) { setError(error.message, 'chatError'); }
}
async function openQuality() {
  try {
    const data = await callApi(`/api/ontologies/${encodeURIComponent(currentOntologyId)}/quality`);
    const root=$('qualityContent'); root.replaceChildren();
    const unnamed = data.unnamed_relation_details || [];
    const unrelatedUnmapped = data.unmapped_relation_ids.filter(id => !data.unnamed_relation_ids.includes(id));
    const records = [
      ['Определения заполнены', `${data.definitions_count} из ${data.nodes} (${Math.round(data.definition_coverage * 100)}%)`, false],
      ['Нет определения', data.missing_definitions.length ? data.missing_definitions.slice(0, 15).join('; ') : 'Все понятия имеют определения', data.missing_definitions.length > 0],
      ['Связи без названия', unnamed.length
        ? unnamed.map(r => `№${r.id}: «${r.source}» → «${r.destination}»`).join('\n')
        : 'Не обнаружены', unnamed.length > 0],
      ['Другие связи вне учебного маршрута', unrelatedUnmapped.length ? `ID: ${unrelatedUnmapped.join(', ')}` : 'Нет', unrelatedUnmapped.length > 0],
      ['Циклы «является видом»', data.is_a_cycles.length ? JSON.stringify(data.is_a_cycles) : 'Не обнаружены', data.is_a_cycles.length > 0]
    ];
    records.forEach(([title, value, warning]) => {
      const item = document.createElement('div'); item.className = `quality-item${warning ? ' warning' : ''}`;
      const strong = document.createElement('strong'); strong.textContent = title;
      const text = document.createElement('span'); text.textContent = value;
      item.append(strong, text); root.append(item);
    });
    $('qualityDialog').showModal();
  } catch(error){ setError(error.message, 'ontologyError'); }
}

$('searchTopics').addEventListener('input', drawTopics);
$('allTopics').addEventListener('click', () => { allTopics.forEach(t => selected.add(t.id)); drawTopics(); });
$('clearTopics').addEventListener('click', () => { selected.clear(); drawTopics(); });
$('ontologySelect').addEventListener('change', () => switchOntology($('ontologySelect').value));
$('uploadButton').addEventListener('click', () => $('ontologyFile').click());
$('ontologyFile').addEventListener('change', uploadOntology);
$('saveRelations').addEventListener('click', saveRelationMapping);
$('startButton').addEventListener('click', start);
$('sendButton').addEventListener('click', send);
$('finishButton').addEventListener('click', () => closeSession(false));
$('forceButton').addEventListener('click', () => closeSession(true));
$('graphButton').addEventListener('click', () => openGraph());
$('graphDepth').addEventListener('change', () => { if ($('graphDialog').open) openGraph(graphNodeId); });
$('closeGraph').addEventListener('click', () => $('graphDialog').close());
$('helpButton').addEventListener('click', () => $('helpDialog').showModal());
$('closeHelp').addEventListener('click', () => $('helpDialog').close());
$('gotItButton').addEventListener('click', () => $('helpDialog').close());
$('qualityButton').addEventListener('click', openQuality);
$('closeQuality').addEventListener('click', () => $('qualityDialog').close());
$('questionCount').addEventListener('input' , () => { if (!currentSession?.active) $('countProgress').textContent = `0 / ${$('questionCount').value || 2}`; });
$('messageInput').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
});
initialize();
