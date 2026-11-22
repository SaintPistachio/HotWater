const get = id => document.getElementById(id);
const say = text => { get('message').textContent = text; };
let csrf = '';
let submitting = false;
const form = get('login');

async function post(path, body) {
  const headers = {'Content-Type':'application/json'};
  if (csrf) headers['X-CSRF-Token'] = csrf;
  const response = await fetch(path, {method:'POST', headers, body:JSON.stringify(body), cache:'no-store'});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(result.error || 'Request failed. Check status before retrying.');
  return result;
}

if (form) {
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      await post('/api/login', {password:get('password').value});
      location.replace('/');
    } catch (error) {
      say(error.message);
    } finally {
      get('password').value = '';
      button.disabled = false;
    }
  });
} else {
  const clock = new Intl.DateTimeFormat('en-US', {year:'numeric', month:'short', day:'numeric',
    hour:'numeric', minute:'2-digit', timeZoneName:'short'});
  get('schedule-zone').textContent = `Times use ${clock.resolvedOptions().timeZone.replaceAll('_', ' ')}.`;
  const states = {queued:'Queued', submitting:'Contacting relay', delivered:'Delivered', acknowledged:'Acknowledged',
    confirmed:'Confirmed by heater', rejected:'Rejected', unresolved:'Outcome unresolved', cancelled:'Cancelled',
    scheduled:'Scheduled', missed:'Missed', expired:'Expired before delivery', blocked:'Blocked', already_set:'Heater was already in this mode'};
  const scheduleFields = ['schedule-mode','schedule-days','schedule-time','schedule-submit'];
  const presetFields = ['preset-name','preset-target','preset-submit'];
  const fields = ['target','temperature-submit','days','vacation-submit','standard', ...scheduleFields, ...presetFields];
  const listSnapshots = new Map();
  let refreshSequence = 0;
  const disable = () => {
    for (const id of fields) get(id).disabled = true;
    for (const button of document.querySelectorAll('#commands button, #schedules button, #presets button')) button.disabled = true;
    listSnapshots.clear();
  };
  const degrees = value => Number.isFinite(value) ? `${value} °F` : 'Unknown';
  const dateTime = seconds => Number.isFinite(seconds) ? clock.format(seconds * 1000) : 'Unknown';
  const modeAction = (mode, days) => mode === 'vacation' ? `Start Vacation for ${days} ${days === 1 ? 'day' : 'days'}` : 'Return to Standard';

  function syncScheduleDays() {
    const standard = get('schedule-mode').value === 'standard';
    get('schedule-duration').hidden = standard;
    get('schedule-days').disabled = standard || get('schedule-mode').disabled;
  }

  function listItem(text) {
    const item = document.createElement('li');
    item.textContent = text;
    return item;
  }

  function listButton(item, label, onClick, disabled = false, context = '') {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = label;
    if (context) button.setAttribute('aria-label', `${label} ${context}`);
    button.disabled = disabled;
    button.addEventListener('click', onClick);
    item.append(' ', button);
  }

  function renderList(id, entries, empty, render, ...dependency) {
    const snapshot = JSON.stringify([entries, empty, ...dependency]);
    if (listSnapshots.get(id) === snapshot) return;
    listSnapshots.set(id, snapshot);
    get(id).replaceChildren(...(entries.length ? entries.map(render) : [listItem(empty)]));
  }

  function renderDiagnostics(current) {
    const diagnostics = current.diagnostics;
    get('operating-state').textContent = {idle:'Idle', heating:'Heating', fault:'Fault'}[diagnostics.operating_state] || 'Unknown';
    get('element').textContent = {upper:'Upper', lower:'Lower', off:'Off'}[diagnostics.element] || 'Unknown';
    get('power').textContent = Number.isFinite(diagnostics.watts) ? `${diagnostics.watts} W` : 'Unknown';
    get('last-reading').textContent = dateTime(diagnostics.last_telemetry);
    get('active-fault').textContent = diagnostics.fault || (diagnostics.faults_available ? 'None reported' : 'Unknown');
    renderList('fault-history', diagnostics.fault_history,
      diagnostics.faults_available ? 'No recent faults reported.' : 'Fault history unavailable.', listItem);
    let vacation = '';
    if (current.mode === 'vacation') {
      const days = current.vacation_days;
      const duration = Number.isInteger(days) && days > 0 ? ` Heater reports ${days} ${days === 1 ? 'day' : 'days'}.` : '';
      vacation = `Vacation active.${duration}`;
      if (current.vacation_since) vacation += ` First observed ${dateTime(current.vacation_since)}.`;
    } else if (current.mode === 'standard' && current.returned_to_standard) {
      vacation = `Return to Standard observed ${dateTime(current.returned_to_standard)}.`;
    }
    get('vacation-status').textContent = vacation;
  }

  async function refresh() {
    const sequence = ++refreshSequence;
    try {
      const response = await fetch('/api/status', {cache:'no-store'});
      if (sequence !== refreshSequence) return;
      if (response.status === 401) { location.replace('/login'); return; }
      if (!response.ok) throw Error('Cannot read heater status.');
      const current = await response.json();
      if (sequence !== refreshSequence) return;
      csrf = current.csrf;
      get('stored').textContent = degrees(current.target);
      get('active').textContent = degrees(current.active_target);
      get('mode').textContent = {standard:'Standard', vacation:'Vacation'}[current.mode] || 'Unverified';
      get('control-status').textContent = current.diagnostics.reason || 'Heater is connected locally.';
      renderDiagnostics(current);
      const targetEnabled = current.ready && current.temperature_enabled && !current.busy && !submitting &&
        current.mode !== 'vacation' && Number.isFinite(current.max);
      get('target').max = Math.min(current.max ?? 150, 150);
      get('target').disabled = get('temperature-submit').disabled = !targetEnabled;
      get('target-note').textContent = !current.temperature_enabled ? 'Temperature control awaits verification on this heater.' :
        current.mode === 'vacation' ? 'Return to Standard to change the tank target.' :
        'The mixing valve sets the outlet temperature independently.';
      const modes = current.ready && current.modes_enabled && !current.busy && !submitting;
      get('days').disabled = get('vacation-submit').disabled = !modes || current.mode === 'vacation';
      get('standard').disabled = !modes || current.mode !== 'vacation';
      get('mode-note').textContent = current.modes_enabled ? 'Vacation lowers the tank target temporarily.' :
        'Vacation and Standard controls await verification on this heater.';
      for (const id of scheduleFields) get(id).disabled = !current.modes_enabled || submitting;
      syncScheduleDays();
      for (const id of presetFields) get(id).disabled = submitting;
      renderList('commands', current.commands, 'No admin commands yet.', command => {
        const action = command.kind === 'setpoint' ? `${command.value} °F target` :
          modeAction(command.value, command.days);
        const item = listItem(`${dateTime(command.created)}. ${action}. ${states[command.status] || command.status}.`);
        if (command.status === 'queued') {
          listButton(item, 'Cancel queued change', () => act('/api/cancel', {id:command.id}), submitting, action);
        }
        return item;
      }, submitting);
      renderList('schedules', current.schedules, 'No scheduled changes.', schedule => {
        const action = modeAction(schedule.mode, schedule.days);
        const detail = schedule.detail ? ` ${schedule.detail}` : '';
        const item = listItem(`${dateTime(schedule.due)}. ${action}. ${states[schedule.status] || schedule.status}.${detail}`);
        if (schedule.status === 'scheduled') {
          listButton(item, 'Cancel schedule', () => act('/api/schedules/cancel', {id:schedule.id}), submitting, action);
        }
        return item;
      }, submitting);
      renderList('presets', current.presets, 'No temperature presets saved.', preset => {
        const item = listItem(`${preset.name}, ${degrees(preset.target)}`);
        listButton(item, 'Apply', () => change('setpoint', preset.target), !targetEnabled || preset.target > current.max, preset.name);
        listButton(item, 'Remove', () => {
          if (window.confirm(`Remove the “${preset.name}” preset?`)) act('/api/presets/delete', {name:preset.name});
        }, submitting, preset.name);
        return item;
      }, submitting, targetEnabled, current.max);
    } catch (error) {
      if (sequence !== refreshSequence) return;
      disable();
      get('control-status').textContent = error.message;
    }
  }

  async function act(path, body) {
    if (submitting) return;
    submitting = true;
    disable();
    try {
      const result = await post(path, body);
      say(result.message || 'Queued change cancelled.');
      return true;
    } catch (error) {
      say(error.message);
      return false;
    } finally {
      submitting = false;
      await refresh();
    }
  }

  function change(kind, value, days) {
    const descriptions = {setpoint:`Set the tank target to ${value} °F?`, vacation:`Start Vacation for ${days} day(s)?`, standard:'Return to Standard heating?'};
    if (window.confirm(descriptions[kind === 'setpoint' ? kind : value])) {
      act('/api/commands', {id:crypto.randomUUID().replaceAll('-', ''),kind,value,days});
    }
  }
  get('target-form').addEventListener('submit', event => { event.preventDefault(); change('setpoint', get('target').valueAsNumber); });
  get('vacation-form').addEventListener('submit', event => { event.preventDefault(); change('mode', 'vacation', get('days').valueAsNumber); });
  get('standard').addEventListener('click', () => change('mode', 'standard'));
  get('schedule-mode').addEventListener('change', syncScheduleDays);
  get('schedule-form').addEventListener('submit', async event => {
    event.preventDefault();
    const input = get('schedule-time').value;
    const clockTime = new Date(input);
    const wallClock = new Date(clockTime.getTime() - clockTime.getTimezoneOffset() * 60000);
    if (!Number.isFinite(clockTime.getTime()) || wallClock.toISOString().slice(0, 16) !== input) {
      say('Choose a valid local date and time.');
      return;
    }
    const due = Math.round(clockTime.getTime() / 1000);
    const mode = get('schedule-mode').value;
    const days = mode === 'vacation' ? get('schedule-days').valueAsNumber : null;
    if (window.confirm(`${modeAction(mode, days)} on ${dateTime(due)}?`) &&
        await act('/api/schedules', {id:crypto.randomUUID().replaceAll('-', ''), due, mode, days})) {
      get('schedule-time').value = '';
    }
  });
  get('preset-form').addEventListener('submit', async event => {
    event.preventDefault();
    const name = get('preset-name').value;
    const target = get('preset-target').valueAsNumber;
    if (await act('/api/presets', {name, target})) event.target.reset();
  });
  get('signout').addEventListener('click', async () => {
    try { await post('/api/logout', {}); location.replace('/login'); } catch (error) { say(error.message); }
  });
  async function poll() {
    await refresh();
    setTimeout(poll, 5000);
  }
  poll();
}
