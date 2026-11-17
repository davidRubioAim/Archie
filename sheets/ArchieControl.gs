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
 *   Fila 3  (B..AH): estadio (dropdown de venues del Master Atlas)
 *   Fila 4+ (B..AH): códigos de campaña de ese estadio
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


// ====================================================================== menú
function onOpen() {
  SpreadsheetApp.getUi().createMenu('Archie')
    .addItem('▶ Render new campaigns', 'menuRenderNew')
    .addItem('↻ Re-render selected cells', 'menuRerenderSelection')
    .addItem('🔍 Check Elements now', 'menuRecheck')
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

function menuRecheck() {
  PropertiesService.getScriptProperties().setProperty(P_RECHECK, String(Date.now()));
  SpreadsheetApp.getActive().toast('Archie will check the Elements on the NAS in a few seconds.', 'Archie', 6);
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
      case 'paint': return json_(paint_(req));
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
    reset: props.getProperty(P_RESET) || ''
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

function child_(parent, name) {
  var it = parent.getFoldersByName(name);
  return it.hasNext() ? it.next() : parent.createFolder(name);
}
