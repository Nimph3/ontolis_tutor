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
  if (role === 'bot' && result?.result === 'answered') {
    const details = document.createElement('details'); details.className = 'source-details';
    const summary = document.createElement('summary'); summary.textContent = 'Источники и путь по графу'; details.append(summary);
    const ul = document.createElement('ul');
    const relationText = (r) => `${r.type}${r.role && r.role !== r.type ? ` → ${r.role}` : ''}`;
    if (result.traversed_via) {
      const r = result.traversed_via;
      addLi(ul, `Шаг обхода: связь #${r.id} (${relationText(r)}): ${r.source} [#${r.source_id}] → ${r.destination} [#${r.destination_id}]`);
    } else addLi(ul, `Начальный узел #${result.answered_node.id}`);
    for (const item of result.sources) {
      if (item.relation) {
        const r = item.relation;
        addLi(ul, `Связь #${r.id} (${relationText(r)}): ${r.source} → ${r.destination}; узел #${item.node_id}: ${item.field}`);
      } else addLi(ul, `Узел #${item.node_id}: ${item.field}`);
    }
    details.append(ul); outer.append(details);
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
  $('currentHint').textContent = picking ? (state.branches_completed ? 'Ветка пройдена · можно начать другую' : 'Можно начать с любой выбранной темы или её подтемы') : 'Обходим выбранную ветку в глубину (DFS)';
  $('topicIndex').textContent = picking ? (state.branches_completed ? '✓' : '—') : `${state.current_index + 1} / ${state.course_length}`;
  $('countProgress').textContent = `${state.questions_answered} / ${state.question_count}`;
  $('progressBar').style.width = `${Math.min(100, Math.round(state.questions_answered / state.question_count * 100))}%`;
  $('progressHint').textContent = state.active ? (picking ? `Доступно ${state.allowed_count} понятий · завершено веток: ${state.branches_completed}` : `Ветка ${state.branch_number}: ${state.branch_answered} из ${state.course_length} · осталось до минимума: ${state.remaining}`) : 'Можно начать новую сессию';
  $('finishButton').disabled = !state.can_finish;
  $('forceButton').disabled = !state.active;
  $('composerStatus').textContent = !state.active ? 'Настройки доступны' : picking ? 'Свободный выбор стартового узла' : `DFS · ветка ${state.branch_number} · шаг ${state.current_index + 1}/${state.course_length}`;
  $('chatMode').textContent = picking ? 'Режим: выбор темы' : 'Режим: обход ветки (DFS)';
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
  $('finishButton').disabled = true; $('forceButton').disabled = true;
  $('composerStatus').textContent = 'Настройки доступны до старта';
  $('messages').replaceChildren();
  showMessage('bot', 'Выберите темы слева и начните с любого понятия внутри выбранной области. После первого вопроса последовательно пройдём его ветку.');
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
    if (item.role === 'ignore') ignored += item.count;
  }
  $('relationWarning').textContent = ignored ? `· ${ignored} без обхода` : '';
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
    showMessage('bot', `Сессия по области «${data.ontology_title}» началась. Вы можете задать первый вопрос по любому из ${data.allowed_count} доступных понятий — это определит начало DFS-ветки. После её полного прохождения снова можно будет свободно выбрать тему. Минимум для обычного завершения: ${count} зачтённых вопросов.`);
    $('messageInput').focus();
  } catch (error) { setError(error.message); $('startButton').disabled = false; }
}
async function send() {
  if (!currentSession?.active || isBusy) return;
  const question = $('messageInput').value.trim();
  if (!question) { setError('Введите вопрос.', 'chatError'); return; }
  setError('', 'chatError'); isBusy = true; setComposer(false);
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
  } catch (error) { setError(error.message, 'chatError'); }
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
      showMessage('bot', `Учебная сессия восстановлена в области «${restored.ontology_title}». ${restored.mode === 'choose_start' ? 'Можно выбрать стартовый узел новым вопросом.' : `Продолжаем с темы «${restored.current.name}».`} Задано ${restored.questions_answered} из ${restored.question_count} вопросов.`);
    }
  } catch (error) { setError(error.message, 'ontologyError'); }
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
$('questionCount').addEventListener('input', () => { if (!currentSession?.active) $('countProgress').textContent = `0 / ${$('questionCount').value || 2}`; });
$('messageInput').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); send(); }
});
initialize();
