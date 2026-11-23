/**
 * ArchieControl.gs  (Archie v0.87 · worker silencioso)
 *
 * Script de la hoja "AIM Sport - Archie Campaigns". Hace de puente entre la hoja y el worker de Archie
 * (Core/archie_worker.py) que corre en el PC del estudio:
 *   - Menú "Archie" para lanzar el render de las campañas nuevas, re-renderizar una selección o
 *     forzar la comprobación de ELEMENTS.
 *   - Web app (doPost) protegida con una clave: el worker lee la rejilla, pinta colores / notas / progreso,
 *     pide la carpeta de Drive de cada entrega y un token temporal para subir los archivos.
 *   - onEdit: si alguien cambia un código o un estadio, se borra el color y la nota de esa celda
 *     para que no quede un estado antiguo.
 *
 * Estructura de la pestaña de campañas (gid SHEET_ID, ahora "Render Campaigns"):
 *   Fila 1  (B..AH): texto de progreso por estadio      A2: estado del worker
 *   Fila 2  (B..AH): barra de progreso por estadio
 *   Fila 3  (B..AH): estadio (dropdown que mantiene el worker con los perfiles de Archie: los estadios de 2 rows
 *                    salen una sola vez, como en la app; la lista vive en la pestaña oculta "Archie Stadiums")
 *   Fila 4+ (B..AH): códigos de campaña de ese estadio. Con varias versiones (Elements\NATIONAL, \REGIONAL...)
 *                    se renderizan todas; "CÓDIGO VERSIÓN" (p.ej. AUDI-MLSX-261001 NATIONAL) deja solo esa, y
 *                    Archie ▸ Choose versions to render… lanza las que se marquen de la celda seleccionada.
 *
 * Instalación: ver sheets/LEEME_WORKER.md
 */

var SHEET_ID = 1272902361;   // gid de la pestaña de campañas (se busca por id: da igual cómo se llame)
var STADIUM_ROW = 3;
var FIRST_CODE_ROW = 4;
var FIRST_COL = 2;            // B
var LAST_COL = 34;            // AH
var PROGRESS_TEXT_ROW = 1;
var PROGRESS_BAR_ROW = 2;
var STATUS_CELL = 'A2';
var DRIVE_ROOT_ID = '1jnsGaQQewvq3LDWdi7y0NTBztmIWkDnO';   // carpeta de entregas de Archie

// Colores que pinta el worker (archie_worker.py). Cualquier otro (p.ej. las bandas alternas) cuenta como "sin color".
var ARCHIE_COLORS = ['#ea9999', '#f9cb9c', '#cfe2f3', '#ffe599', '#b6d7a8', '#d9d9d9'];

var P_SECRET = 'ARCHIE_SECRET';
var P_LAUNCH = 'ARCHIE_LAUNCH';
var P_RECHECK = 'ARCHIE_RECHECK';
var P_RESET = 'ARCHIE_RESET';
var P_DELETE = 'ARCHIE_DELETE';
var STADIUMS_TAB = 'Archie Stadiums';


// ====================================================================== menú
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Archie')
    .addItem('▶ Render new campaigns', 'menuRenderNew')
    .addItem('↻ Re-render selected cells', 'menuRerenderSelection')
    .addItem('🎯 Choose versions to render…', 'menuChooseVersions')
    .addItem('🔍 Check Elements now', 'menuRecheck')
    .addItem('🗑 Delete files of selected cells', 'menuDeleteSelection')
    .addItem('🧹 Reset campaign codes', 'menuReset')
    .addSeparator()
    .addItem('⚙ Show worker key (setup)', 'menuSetup')
    .addToUi();
}

function grid_() {
  var sheets = SpreadsheetApp.getActive().getSheets();
  for (var i = 0; i < sheets.length; i++) {
    if (sheets[i].getSheetId() === SHEET_ID) return sheets[i];
  }
  throw new Error('Campaign tab (gid ' + SHEET_ID + ') not found');
}

function codeCells_(sh, onlyRange) {
  var lastRow = Math.max(sh.getLastRow(), FIRST_CODE_ROW);
  var nCols = LAST_COL - FIRST_COL + 1;
  var stadiums = sh.getRange(STADIUM_ROW, FIRST_COL, 1, nCols).getDisplayValues()[0];
  var vals = sh.getRange(FIRST_CODE_ROW, FIRST_COL, lastRow - FIRST_CODE_ROW + 1, nCols).getDisplayValues();
  var out = [];
  for (var r = 0; r < vals.length; r++) {
    for (var c = 0; c < nCols; c++) {
      var code = String(vals[r][c] || '').trim();
      if (!code) continue;
      var row = FIRST_CODE_ROW + r, col = FIRST_COL + c;
      if (onlyRange && !inRange_(onlyRange, row, col)) continue;
      out.push({row: row, col: col, code: code, stadium: String(stadiums[c] || '').trim()});
    }
  }
  return out;
}

function inRange_(rg, row, col) {
  return row >= rg.getRow() && row <= rg.getLastRow() && col >= rg.getColumn() && col <= rg.getLastColumn();
}

function user_() {
  try { return Session.getActiveUser().getEmail() || ''; } catch (e) { return ''; }
}

function menuRenderNew() {
  var cells = codeCells_(grid_());
  var launch = {id: String(Date.now()), by: user_(), at: new Date().toISOString(), force: false, cells: cells};
  PropertiesService.getScriptProperties().setProperty(P_LAUNCH, JSON.stringify(launch));
  SpreadsheetApp.getActive().toast(
    cells.length + ' code(s) sent to Archie. Campaigns with Elements ready will turn yellow and start rendering.',
    'Archie', 8);
}

function menuRerenderSelection() {
  var sh = SpreadsheetApp.getActiveSheet();
  var sel = sh.getActiveRange();
  if (!sel || sh.getSheetId() !== SHEET_ID) {
    SpreadsheetApp.getUi().alert('Select the campaign cells to render again in the ' + grid_().getName() + ' tab.');
    return;
  }
  var cells = codeCells_(sh, sel);
  if (!cells.length) {
    SpreadsheetApp.getUi().alert('The selection has no campaign codes.');
    return;
  }
  var launch = {id: String(Date.now()), by: user_(), at: new Date().toISOString(), force: true, cells: cells};
  PropertiesService.getScriptProperties().setProperty(P_LAUNCH, JSON.stringify(launch));
  SpreadsheetApp.getActive().toast(cells.length + ' campaign(s) will be rendered again.', 'Archie', 8);
}

// ---------------------------------------------------------------- versiones (NATIONAL / REGIONAL, C1 / C2...)
function versionsOfNote_(note) {
  var m = /·\s*\d+ versions \(([^)]*)\)/.exec(String(note || ''));
  return m ? m[1].split(',').map(function (v) { return v.trim(); }).filter(String) : [];
}

function menuChooseVersions() {
  var ui = SpreadsheetApp.getUi();
  var sh = SpreadsheetApp.getActiveSheet();
  var rg = sh.getActiveRange();
  var cells = (rg && sh.getSheetId() === SHEET_ID) ? codeCells_(sh, rg) : [];
  if (cells.length !== 1) {
    ui.alert('Select ONE campaign cell (with its code) in the ' + grid_().getName() + ' tab.');
    return;
  }
  var c = cells[0];
  var versions = versionsOfNote_(sh.getRange(c.row, c.col).getNote());
  if (!versions.length) {
    ui.alert(c.code + ' has a single version (or Archie has not checked its Elements yet).\n\n' +
             'Use ▶ Render new campaigns or ↻ Re-render selected cells.');
    return;
  }
  var t = HtmlService.createTemplate(
    '<div style="font:13px Arial,sans-serif">' +
    '<p><b><?= code ?></b> · <?= stadium ?></p>' +
    '<? for (var i = 0; i < versions.length; i++) { ?>' +
    '<label style="display:block;margin:6px 0"><input type="checkbox" name="v" value="<?= versions[i] ?>"> ' +
    '<?= versions[i] ?></label><? } ?>' +
    '<p style="color:#666">The marked versions are rendered (again if they were already delivered).</p>' +
    '<button id="go" onclick="go()">Render</button> <button onclick="google.script.host.close()">Cancel</button>' +
    '<script>function go(){var v=[].slice.call(document.querySelectorAll("input:checked")).map(function(e){return e.value});' +
    'if(!v.length){alert("Mark at least one version.");return;}document.getElementById("go").disabled=true;' +
    'google.script.run.withSuccessHandler(function(){google.script.host.close()})' +
    '.launchVersions(<?= row ?>,<?= col ?>,v);}</script></div>');
  t.code = c.code; t.stadium = c.stadium; t.versions = versions; t.row = c.row; t.col = c.col;
  ui.showModalDialog(t.evaluate().setWidth(320).setHeight(140 + 30 * versions.length), 'Choose versions to render');
}

function launchVersions(row, col, versions) {
  var sh = grid_();
  var c = codeCells_(sh, sh.getRange(row, col))[0];
  if (!c || !versions || !versions.length) return;
  c.variants = versions.map(function (v) { return v === 'main' ? '' : v; });
  var launch = {id: String(Date.now()), by: user_(), at: new Date().toISOString(), force: true, cells: [c]};
  PropertiesService.getScriptProperties().setProperty(P_LAUNCH, JSON.stringify(launch));
  SpreadsheetApp.getActive().toast(c.code + ': ' + versions.join(', ') + ' will be rendered.', 'Archie', 8);
}

function menuRecheck() {
  PropertiesService.getScriptProperties().setProperty(P_RECHECK, String(Date.now()));
  SpreadsheetApp.getActive().toast('Archie will check the Elements on the NAS in a few seconds.', 'Archie', 6);
}

function menuDeleteSelection() {
  var ui = SpreadsheetApp.getUi();
  var sh = SpreadsheetApp.getActiveSheet();
  var sel = sh.getActiveRange();
  if (!sel || sh.getSheetId() !== SHEET_ID) {
    ui.alert('Select the campaign cells whose files you want to delete in the ' + grid_().getName() + ' tab.');
    return;
  }
  var cells = codeCells_(sh, sel).filter(function (c) { return c.stadium; });
  if (!cells.length) {
    ui.alert('The selection has no campaign codes with a stadium.');
    return;
  }
  var working = cells.filter(function (c) {
    return String(sh.getRange(c.row, c.col).getBackground()).toLowerCase() === '#ffe599';
  });
  var lista = cells.slice(0, 15).map(function (c) { return '• ' + c.code + ' · ' + c.stadium; }).join('\n');
  if (cells.length > 15) lista += '\n… and ' + (cells.length - 15) + ' more';
  var msg = 'The System Files, 3D Previews and JSON of these ' + cells.length + ' campaign(s) will be deleted:\n\n' +
    lista + '\n\n' +
    '• NAS: the files are deleted from BCSA\\<Stadium> (this cannot be undone).\n' +
    '• Google Drive: the stadium folder is moved to the trash.\n\n' +
    'The codes stay in the sheet. Once Archie has deleted the files, the cells turn blue (ready to render again ' +
    'with ▶ Render new campaigns) or red if the Elements are not complete.';
  if (working.length) msg += '\n\n⚠ ' + working.length + ' of them are rendering right now (yellow) and will be skipped.';
  if (ui.alert('Delete the files of these campaigns?', msg, ui.ButtonSet.YES_NO) !== ui.Button.YES) return;

  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var props = PropertiesService.getScriptProperties();
    var prev = props.getProperty(P_DELETE);     // si había un borrado pendiente se suma a este
    var todas = (prev ? JSON.parse(prev).cells : []).concat(cells);
    props.setProperty(P_DELETE, JSON.stringify({id: String(Date.now()), by: user_(), at: new Date().toISOString(),
                                                cells: todas}));
  } finally {
    lock.releaseLock();
  }
  cells.forEach(function (c) { sh.getRange(c.row, c.col).clearNote(); });   // fuera el link antiguo
  SpreadsheetApp.getActive().toast('Archie will delete the files of ' + cells.length +
    ' campaign(s) in a few seconds.', 'Archie', 8);
}

function menuReset() {
  var ui = SpreadsheetApp.getUi();
  var sh = grid_();
  var cells = codeCells_(sh);
  if (!cells.length) {
    ui.alert('There are no campaign codes to reset.');
    return;
  }
  var lastRow = Math.max(sh.getLastRow(), FIRST_CODE_ROW);
  var nCols = LAST_COL - FIRST_COL + 1;
  var rg = sh.getRange(FIRST_CODE_ROW, FIRST_COL, lastRow - FIRST_CODE_ROW + 1, nCols);
  var working = rg.getBackgrounds().some(function (row) {
    return row.some(function (bg) { return String(bg).toLowerCase() === '#ffe599'; });
  });
  var msg = 'This deletes the ' + cells.length + ' campaign code(s) below the stadiums, with their colors and notes. ' +
    'The stadiums in row 3 are kept.\n\nDelivered files stay on the NAS and in Drive: if a code is added again ' +
    'for the same stadium, it turns green with its existing link (no new render).';
  if (working) msg += '\n\n⚠ Some campaigns are still rendering (yellow). They will finish and be delivered, ' +
    'but their cells will no longer be updated.';
  if (ui.alert('Reset campaign codes?', msg, ui.ButtonSet.OK_CANCEL) !== ui.Button.OK) return;
  rg.clearContent();
  rg.setBackground(null);
  rg.clearNote();
  sh.getRange(PROGRESS_TEXT_ROW, FIRST_COL, 2, nCols).clearContent();
  var props = PropertiesService.getScriptProperties();
  props.deleteProperty(P_LAUNCH);
  props.setProperty(P_RESET, String(Date.now()));
  SpreadsheetApp.getActive().toast(cells.length + ' code(s) removed.', 'Archie', 6);
}

function menuSetup() {
  var props = PropertiesService.getScriptProperties();
  var secret = props.getProperty(P_SECRET);
  if (!secret) {
    secret = Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, '');
    props.setProperty(P_SECRET, secret);
  }
  // Toca DriveApp aquí para que el script pida el permiso de Drive al autorizarlo
  var root = DriveApp.getFolderById(DRIVE_ROOT_ID).getName();
  SpreadsheetApp.getUi().alert('Archie worker key',
    'Copy this line into Config/.env on the Archie PC:\n\nARCHIE_SHEET_SECRET=' + secret +
    '\n\nDelivery folder: ' + root, SpreadsheetApp.getUi().ButtonSet.OK);
}


// ====================================================================== onEdit
function onEdit(e) {
  if (!e || !e.range) return;
  var sh = e.range.getSheet();
  if (sh.getSheetId() !== SHEET_ID) return;
  var rg = e.range;
  var c0 = Math.max(rg.getColumn(), FIRST_COL), c1 = Math.min(rg.getLastColumn(), LAST_COL);
  if (c0 > c1) return;
  if (rg.getRow() <= STADIUM_ROW && rg.getLastRow() >= STADIUM_ROW) {
    // Estadio cambiado: los estados de toda la columna dejan de valer
    var lastRow = Math.max(sh.getLastRow(), FIRST_CODE_ROW);
    var col = sh.getRange(FIRST_CODE_ROW, c0, lastRow - FIRST_CODE_ROW + 1, c1 - c0 + 1);
    col.setBackground(null);
    col.clearNote();
    sh.getRange(PROGRESS_TEXT_ROW, c0, 2, c1 - c0 + 1).clearContent();
  }
  var r0 = Math.max(rg.getRow(), FIRST_CODE_ROW), r1 = rg.getLastRow();
  if (r0 <= r1) {
    var cells = sh.getRange(r0, c0, r1 - r0 + 1, c1 - c0 + 1);
    cells.setBackground(null);
    cells.clearNote();
  }
}


// ====================================================================== web app (worker)
function doPost(e) {
  var req;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    return json_({ok: false, error: 'bad json'});
  }
  var secret = PropertiesService.getScriptProperties().getProperty(P_SECRET);
  if (!secret || req.secret !== secret) return json_({ok: false, error: 'unauthorized'});
  try {
    switch (req.action) {
      case 'poll': return json_(poll_());
      case 'ack_launch': return json_(ackLaunch_(req.id));
      case 'ack_delete': return json_(ackDelete_(req.id));
      case 'drive_delete': return json_(driveDelete_(req.code, req.stadium));
      case 'paint': return json_(paint_(req));
      case 'stadium_list': return json_(stadiumList_(req.names || []));
      case 'drive_folder': return json_(driveFolder_(req.code, req.stadium, req.replace || [], req.sub || ''));
      case 'token': return json_({ok: true, token: ScriptApp.getOAuthToken()});
      default: return json_({ok: false, error: 'unknown action'});
    }
  } catch (err) {
    return json_({ok: false, error: String(err)});
  }
}

function doGet() {
  return ContentService.createTextOutput('Archie control sheet bridge: OK');
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

function poll_() {
  var sh = grid_();
  var lastRow = Math.max(sh.getLastRow(), FIRST_CODE_ROW);
  var nCols = LAST_COL - FIRST_COL + 1;
  var nRows = lastRow - FIRST_CODE_ROW + 1;
  var stadiums = sh.getRange(STADIUM_ROW, FIRST_COL, 1, nCols).getDisplayValues()[0];
  var rg = sh.getRange(FIRST_CODE_ROW, FIRST_COL, nRows, nCols);
  var vals = rg.getDisplayValues(), bgs = rg.getBackgrounds(), notes = rg.getNotes();
  var cells = [];
  for (var r = 0; r < nRows; r++) {
    for (var c = 0; c < nCols; c++) {
      var code = String(vals[r][c] || '').trim();
      var bg = String(bgs[r][c] || '').toLowerCase();
      if (ARCHIE_COLORS.indexOf(bg) < 0) bg = '#ffffff';
      var note = notes[r][c] || '';
      if (!code && !note && bg === '#ffffff') continue;
      cells.push({row: FIRST_CODE_ROW + r, col: FIRST_COL + c, code: code, bg: bg, note: note});
    }
  }
  var progress = sh.getRange(PROGRESS_TEXT_ROW, FIRST_COL, 2, nCols).getFormulas();
  var progressText = sh.getRange(PROGRESS_TEXT_ROW, FIRST_COL, 1, nCols).getDisplayValues()[0];
  var props = PropertiesService.getScriptProperties();
  var launch = props.getProperty(P_LAUNCH);
  return {
    ok: true,
    stadiums: stadiums.map(function (s, i) { return {col: FIRST_COL + i, name: String(s || '').trim()}; }),
    cells: cells,
    progress: progressText.map(function (t, i) {
      return {col: FIRST_COL + i, text: t, bar: progress[1][i]};
    }),
    launch: launch ? JSON.parse(launch) : null,
    recheck: props.getProperty(P_RECHECK) || '',
    reset: props.getProperty(P_RESET) || '',
    'delete': props.getProperty(P_DELETE) ? JSON.parse(props.getProperty(P_DELETE)) : null
  };
}

function ackLaunch_(id) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var props = PropertiesService.getScriptProperties();
    var launch = props.getProperty(P_LAUNCH);
    if (launch && JSON.parse(launch).id === String(id)) props.deleteProperty(P_LAUNCH);
    props.deleteProperty(P_RECHECK);
  } finally {
    lock.releaseLock();
  }
  return {ok: true};
}

function ackDelete_(id) {
  var lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    var props = PropertiesService.getScriptProperties();
    var pend = props.getProperty(P_DELETE);
    if (pend && JSON.parse(pend).id === String(id)) props.deleteProperty(P_DELETE);
  } finally {
    lock.releaseLock();
  }
  return {ok: true};
}

/**
 * Manda a la papelera la carpeta <raíz>/<CÓDIGO>/<Estadio> (con sus versiones). Si la carpeta del código
 * se queda vacía, también. No crea nada: si no existe, devuelve trashed 0.
 */
function driveDelete_(code, stadium) {
  var root = DriveApp.getFolderById(DRIVE_ROOT_ID);
  var fCode = live_(root.getFoldersByName(code));
  if (!fCode) return {ok: true, trashed: 0};
  var n = 0, f;
  while ((f = live_(fCode.getFoldersByName(stadium)))) { f.setTrashed(true); n++; }
  if (!live_(fCode.getFolders()) && !live_(fCode.getFiles())) fCode.setTrashed(true);
  return {ok: true, trashed: n};
}

/**
 * req.cells:    [{row, col, code, bg|null, note}]   solo se pinta si la celda sigue teniendo ese código
 * req.progress: [{col, text, value}]                 value 0..1 o null (sin barra)
 * req.status:   texto para STATUS_CELL
 */
function paint_(req) {
  var sh = grid_();
  var skipped = 0;
  (req.cells || []).forEach(function (c) {
    var cell = sh.getRange(c.row, c.col);
    if (String(cell.getDisplayValue() || '').trim() !== String(c.code || '')) { skipped++; return; }
    cell.setBackground(c.bg || null);
    if (c.note) cell.setNote(c.note); else cell.clearNote();
  });
  (req.progress || []).forEach(function (p) {
    sh.getRange(PROGRESS_TEXT_ROW, p.col).setValue(p.text || '');
    var bar = sh.getRange(PROGRESS_BAR_ROW, p.col);
    if (p.value === null || p.value === undefined) {
      bar.clearContent();
    } else {
      var v = Math.max(0, Math.min(1, Number(p.value) || 0));
      var color = v >= 1 ? '#6AA84F' : '#F1C232';
      bar.setFormula('=SPARKLINE(' + v.toFixed(3) + ',{"charttype","bar";"max",1;"color1","' + color + '"})');
    }
  });
  if (req.status) sh.getRange(STATUS_CELL).setValue(req.status);
  return {ok: true, skipped: skipped};
}

/**
 * Carpeta <raíz>/<CÓDIGO>/<Estadio>[/<versión C1, C2...>] (la crea si no existe) y manda a la papelera los
 * archivos de `replace` que ya estuvieran en ella. Devuelve el id donde subir y la URL de la carpeta del estadio
 * (el link de la celda: desde ahí se ven todas las versiones, pero nada más de ARCHIE-BCSAs).
 */
function driveFolder_(code, stadium, replace, sub) {
  var root = DriveApp.getFolderById(DRIVE_ROOT_ID);
  var fStadium = child_(child_(root, code), stadium);
  var target = sub ? child_(fStadium, sub) : fStadium;
  (replace || []).forEach(function (name) {
    var it = target.getFilesByName(name);
    while (it.hasNext()) it.next().setTrashed(true);
  });
  return {ok: true, id: target.getId(), url: fStadium.getUrl()};
}

/**
 * Dropdown de la fila 3 = perfiles de Archie (los de 2 rows una sola vez, como en la app). La lista se guarda
 * en la pestaña oculta STADIUMS_TAB. Las celdas con 'X - 1st' / 'X - 2nd' pasan a 'X' (el worker ya las trataba
 * como el mismo estadio, así que sus entregas no cambian).
 */
function stadiumList_(names) {
  if (!names.length) return {ok: true, fixed: 0};
  var ss = SpreadsheetApp.getActive();
  var tab = ss.getSheetByName(STADIUMS_TAB) || ss.insertSheet(STADIUMS_TAB);
  tab.clearContents();
  tab.getRange(1, 1, names.length, 1).setValues(names.map(function (n) { return [n]; }));
  if (!tab.isSheetHidden()) tab.hideSheet();
  var sh = grid_();
  var nCols = LAST_COL - FIRST_COL + 1;
  var row = sh.getRange(STADIUM_ROW, FIRST_COL, 1, nCols);
  var rule = SpreadsheetApp.newDataValidation()
    .requireValueInRange(tab.getRange(1, 1, names.length, 1), true).setAllowInvalid(true).build();
  row.setDataValidation(rule);
  var set = {};
  names.forEach(function (n) { set[n] = true; });
  var vals = row.getValues(), fixed = 0;
  vals[0] = vals[0].map(function (v) {
    var base = String(v || '').replace(/\s*-\s*(1st|2nd)\s*$/i, '').trim();
    if (base !== String(v || '').trim() && set[base]) { fixed++; return base; }
    return v;
  });
  if (fixed) row.setValues(vals);
  return {ok: true, fixed: fixed};
}

function child_(parent, name) {
  var f = live_(parent.getFoldersByName(name));
  return f || parent.createFolder(name);
}

function live_(it) {        // primera carpeta que no esté en la papelera
  while (it.hasNext()) { var f = it.next(); if (!f.isTrashed()) return f; }
  return null;
}
