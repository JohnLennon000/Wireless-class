/* Сетевой модуль ученического ноутбука.
 * Написан в стиле ES5, чтобы работать в старых браузерах (Windows 7).
 * Подключается сервером в конец страницы теста. Исходный тест не изменён. */
(function () {
  'use strict';

  var DEV_KEY = 'yn_device_id';
  var OUT_KEY = 'yn_outbox';
  var cfg = { show_key: false, heartbeat_sec: 3 };
  var deviceId = '';
  var connected = false;
  var outboxMem = [];
  var sending = false;

  function $(id) { return document.getElementById(id); }

  function lsGet(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } }
  function lsSet(k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* ignore */ } }

  function http(method, url, body, cb) {
    var x = new XMLHttpRequest();
    x.open(method, url, true);
    x.timeout = 4000;
    x.setRequestHeader('Content-Type', 'application/json');
    x.onreadystatechange = function () {
      if (x.readyState !== 4) { return; }
      var ok = x.status >= 200 && x.status < 300, data = null;
      try { data = JSON.parse(x.responseText); } catch (e) { /* ignore */ }
      if (cb) { cb(ok, data); }
    };
    x.send(body ? JSON.stringify(body) : null);
  }

  /* ---------- номер ноутбука ---------- */
  function pad2(n) { return (n < 10 ? '0' : '') + n; }

  function askDeviceId(force) {
    var cur = deviceId, text;
    for (;;) {
      text = window.prompt('Введите номер ноутбука (число от 1 до 99):', cur ? cur.replace('NB-', '') : '');
      if (text === null) { if (cur) { return cur; } continue; }
      var n = parseInt(text, 10);
      if (n >= 1 && n <= 99) { return 'NB-' + pad2(n); }
    }
  }

  function initDeviceId() {
    var m = /[?&]n=(\d{1,2})/.exec(window.location.search);
    if (m) { lsSet(DEV_KEY, 'NB-' + pad2(parseInt(m[1], 10))); }
    deviceId = lsGet(DEV_KEY) || '';
    if (!deviceId) {
      deviceId = askDeviceId();
      lsSet(DEV_KEY, deviceId);
    }
  }

  /* ---------- индикатор связи ---------- */
  var badge;
  function makeBadge() {
    badge = document.createElement('div');
    badge.style.cssText = 'position:fixed;right:8px;bottom:8px;z-index:9999;padding:6px 12px;' +
      'border-radius:16px;font:600 13px Segoe UI,Tahoma,sans-serif;color:#fff;cursor:pointer;' +
      'box-shadow:0 1px 4px rgba(0,0,0,.4)';
    badge.title = 'Нажмите, чтобы сменить номер ноутбука';
    badge.onclick = function () {
      if (currentState() === 'testing') { window.alert('Нельзя менять номер во время теста.'); return; }
      deviceId = askDeviceId(true);
      lsSet(DEV_KEY, deviceId);
      paintBadge();
    };
    document.body.appendChild(badge);
    paintBadge();
  }
  function paintBadge() {
    if (!badge) { return; }
    badge.style.background = connected ? '#16a34a' : '#dc2626';
    badge.textContent = deviceId + (connected ? ' · связь есть' : ' · нет связи');
  }

  /* ---------- состояние теста ---------- */
  function hidden(id) { var e = $(id); return !e || e.className.indexOf('hidden') !== -1; }

  function currentState() {
    if (!hidden('screenQuiz')) { return 'testing'; }
    if (!hidden('screenResult')) { return 'done'; }
    return 'idle';
  }

  function answeredCount() {
    var c = $('questionsContainer');
    return c ? c.querySelectorAll('input[type=radio]:checked').length : 0;
  }

  function heartbeat() {
    var st = currentState(), fio = '', group = '', total = 0, left = 0;
    try {
      if (st !== 'idle' && currentStudent) { fio = currentStudent.fio; group = currentStudent.group; }
      if (st === 'testing') { total = activeQuestions.length; left = secondsLeft; }
    } catch (e) { /* тест ещё не готов */ }
    http('POST', '/api/heartbeat', {
      device: deviceId, state: st, fio: fio, group: group,
      answered: st === 'testing' ? answeredCount() : 0, total: total, seconds_left: left
    }, function (ok) {
      connected = ok;
      paintBadge();
      if (ok) { flushOutbox(); }
    });
  }

  /* ---------- отправка результата с повтором ---------- */
  function getOutbox() {
    var raw = lsGet(OUT_KEY);
    if (raw === null) { return outboxMem; }
    try { return JSON.parse(raw); } catch (e) { return []; }
  }
  function setOutbox(list) { outboxMem = list; lsSet(OUT_KEY, JSON.stringify(list)); }

  function flushOutbox() {
    var list = getOutbox();
    if (sending || !list.length) { return; }
    sending = true;
    http('POST', '/api/result', list[0], function (ok) {
      sending = false;
      if (ok) {
        var cur = getOutbox();
        cur.shift();
        setOutbox(cur);
        paintStatus(cur.length === 0);
        if (cur.length) { flushOutbox(); }
      } else {
        paintStatus(false);
      }
    });
  }

  function paintStatus(sent) {
    var box = $('ynSendStatus');
    if (!box) { return; }
    box.style.background = sent ? '#dcfce7' : '#fef3c7';
    box.style.color = sent ? '#166534' : '#92400e';
    box.textContent = sent
      ? 'Результат отправлен учителю.'
      : 'Нет связи. Результат сохранён и отправится сам, когда связь появится. Не закрывайте страницу.';
  }

  function queueResult(record, answers) {
    var details = [], i, q, a;
    try {
      for (i = 0; i < activeQuestions.length; i++) {
        q = activeQuestions[i]; a = answers[i] || { chosen: -1 };
        details.push({
          section: q.section, q: q.q, ok: !!a.isCorrect,
          chosen: a.chosen >= 0 ? q.options[a.chosen] : '— нет ответа —',
          correct: q.options[q.correct]
        });
      }
    } catch (e) { /* разбор необязателен */ }
    var list = getOutbox();
    list.push({ uid: deviceId + '-' + record.id, device: deviceId, record: record, details: details });
    setOutbox(list);
    paintStatus(false);
    flushOutbox();
  }

  /* ---------- подключение к исходному тесту ---------- */
  function hook() {
    var origShow = window.showResultScreen;
    window.showResultScreen = function (record, answers) {
      origShow(record, answers);
      var sum = $('resultSummary');
      if (sum && !$('ynSendStatus')) {
        var box = document.createElement('div');
        box.id = 'ynSendStatus';
        box.style.cssText = 'padding:10px 14px;border-radius:6px;margin-bottom:14px;font-weight:600';
        sum.parentNode.insertBefore(box, sum);
      }
      queueResult(record, answers);
      heartbeat();
    };
    var origStart = window.startTest;
    window.startTest = function () { origStart(); heartbeat(); };
  }

  function tidyUi() {
    var ids = ['tabBankBtn', 'tabListBtn'], i, e;
    for (i = 0; i < ids.length; i++) { e = $(ids[i]); if (e) { e.style.display = 'none'; } }
    var btns = document.querySelectorAll('#screenResult button'), b;
    for (i = 0; i < btns.length; i++) {
      b = btns[i];
      if ((b.getAttribute('onclick') || '').indexOf("'list'") !== -1) { b.style.display = 'none'; }
    }
    if (!cfg.show_key) {
      var det = $('resultDetails');
      if (det) {
        det.style.display = 'none';
        var p = det.previousElementSibling;
        while (p && (p.tagName === 'H3' || p.tagName === 'HR')) { p.style.display = 'none'; p = p.previousElementSibling; }
      }
    }
  }

  function start() {
    initDeviceId();
    makeBadge();
    tidyUi();
    hook();
    heartbeat();
    window.setInterval(heartbeat, cfg.heartbeat_sec * 1000);
  }

  function boot() {
    http('GET', '/api/config', null, function (ok, data) {
      if (ok && data) { cfg = data; }
      start();
    });
  }

  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', boot); } else { boot(); }
}());
