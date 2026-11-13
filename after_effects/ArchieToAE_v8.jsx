/*
    AIM AUTO ARCH - AFTER EFFECTS NATIVE SCRIPT
    Versión: 20.0 (Estadios desde Google Sheets + Refresh + Batch Isolation + Tile Stacking + Repetile BG + Center Board Support)
    v22 (Archie v0.61): Logo Under -> las capas de ANIM ZONE quedan por encima de los logos (logos debajo).
    v21 (Archie v0.57): las comps usan el FPS de la hoja (data.fps en modo JSON, columna FPS en modo manual).
    v20 (Archie v0.52): edge cases desde el JSON de ARCHIE (stadium_overrides.json):
      - system_file.tramos: el System File se monta tramo a tramo (px exactos, con máscara por capa)
      - zonas_negras: solidos negros "RESERVED_BRAND" encima de cada zona afectada
    En modo manual (sin JSON) no hay edge cases: para Hoffenheim y similares, exporta el JSON desde ARCHIE.

    Requisito: Preferencias > Scripting y expresiones > "Permitir que los scripts escriban
    archivos y accedan a la red" debe estar activado para descargar la hoja.
*/

(function archieAutoImporter() {
    if (!app.project) return;

    // --- CONFIGURACIÓN DE RUTAS Y CONSTANTES ---
    // JSON legacy: solo se usa como último respaldo si no hay conexión ni copia local.
    var STADIUM_LIBRARY_PATH = "//192.168.20.242/Templates/04_Scripting/06_Auto Arch/versions/Archie_v0.24/Config/stadium_library.json";
    var DEFAULT_MARGIN = 20;
    var LOGO_UNDER_MARGIN = 50;

    // Hoja máster de estadios (la misma que usa ARCHIE: stadium_loader.py)
    var STADIUM_SHEET_ID = "15hfgbbpTAd06OEmSR6Zpad3-xjgN_CiSuLtnS5-ExL0"; // AIM Sport - Atlas Venue Specs
    var STADIUM_SHEET_GID = "1272902361";                                    // pestaña MASTER ATLAS
    var FETCH_TIMEOUT = 8; // segundos
    var CACHE_FOLDER_PATH = Folder.userData.fsName + "/Archie";
    var CACHE_FILE_PATH = CACHE_FOLDER_PATH + "/stadium_cache.json";

    // --- HELPER DE LECTURA Y PARSEO JSON ---
    function parseJSONFile(fileObj, silent) {
        if (!fileObj || !fileObj.exists) return null;
        fileObj.encoding = "UTF-8";
        fileObj.open("r");
        var content = fileObj.read();
        fileObj.close();
        try {
            return eval("(" + content + ")");
        } catch(e) {
            if (!silent) alert("Error al leer el archivo JSON: " + e.toString());
            return null;
        }
    }

    // --- VENTANA DE CARGA ---
    // ScriptUI es síncrono: mientras curl descarga no se puede animar, así que se muestra
    // una paleta con barra de progreso por pasos y se fuerza el repintado en cada uno.
    function showLoadingWindow(title) {
        var w = new Window("palette", "Archie", undefined, { closeButton: false });
        w.orientation = "column";
        w.alignChildren = "fill";
        w.margins = 18;
        w.spacing = 10;
        var lblTitle = w.add("statictext", undefined, title || "Cargando presets de estadios");
        try { lblTitle.graphics.font = ScriptUI.newFont(lblTitle.graphics.font.name, "BOLD", 13); } catch (eF) {}
        var lblStep = w.add("statictext", undefined, "Iniciando...");
        lblStep.preferredSize.width = 340;
        var bar = w.add("progressbar", undefined, 0, 100);
        bar.preferredSize = [340, 10];
        w.center();
        w.show();
        w.update();
        return {
            step: function(text, value) {
                lblStep.text = text;
                if (value !== undefined) bar.value = value;
                w.update();
            },
            close: function() {
                bar.value = 100;
                w.update();
                w.close();
            }
        };
    }

    // =====================================================================
    // --- LIBRERÍA DE ESTADIOS (port de stadium_loader.py) ---
    // Orden de fuentes:
    //   1. Google Sheets (export CSV vía curl / PowerShell) -> si OK, se guarda en caché
    //   2. Copia local (Folder.userData/Archie/stadium_cache.json) -> modo offline
    //   3. stadium_library.json legacy en red
    //   4. Perfil "Custom" mínimo
    // =====================================================================
    var SHEET_CSV_URL = "https://docs.google.com/spreadsheets/d/" + STADIUM_SHEET_ID + "/export?format=csv&gid=" + STADIUM_SHEET_GID;

    // Columna de la hoja (normalizada) -> clave que espera el script
    var COLUMN_MAP = {
        "perimeter width": "Width",
        "perimeter height": "Height",
        "goal line width": "Goal Line Size",
        "centerboard width": "Center Board Width",
        "canvas width": "Canvas Width",
        "canvas height": "Canvas Height",
        "canvas mask": "Canvas Mask",
        "offset x": "Offset X",
        "offset y": "Offset Y",
        "fps": "FPS",
        "competition": "Competition"
    };
    var NAME_COLUMN = "club name";
    var REQUIRED = ["Width", "Height", "Canvas Width", "Canvas Height"];

    function makeDefaults() {
        return {
            "Competition": "NONE",
            "Width": 0, "Height": 0, "Goal Line Size": 0, "Center Board Width": 0,
            "Canvas Width": 0, "Canvas Height": 0, "Canvas Mask": 0,
            "Offset X": 0, "Offset Y": 0, "FPS": 60
        };
    }

    function makeCustomProfile() {
        return {
            "Competition": "NONE", "Width": 39456, "Height": 144, "Goal Line Size": 11232,
            "Center Board Width": 1920, "Canvas Width": 3840, "Canvas Height": 2160,
            "Canvas Mask": 96, "Offset X": 0, "Offset Y": 0, "FPS": 60
        };
    }

    function strTrim(s) { return String(s).replace(/^\s+|\s+$/g, ""); }
    function normHeader(s) { return strTrim(String(s || "").replace(/\s+/g, " ")).toLowerCase(); }
    function cleanName(s) { return strTrim(String(s || "").replace(/\s+/g, " ")); }

    function toInt(value) {
        // '39,456' / '39456' / '-' / '' -> número | null
        var s = strTrim(String(value === undefined || value === null ? "" : value)).replace(/[,\.]/g, "");
        if (!s || s === "-") return null;
        return /^\d+$/.test(s) ? parseInt(s, 10) : null;
    }

    function inferCompetition(name) {
        var upper = name.toUpperCase();
        var tags = ["UWCL", "UECL", "UEL", "UCL"];
        for (var i = 0; i < tags.length; i++) {
            if (new RegExp("(^|[^A-Z0-9_])" + tags[i] + "([^A-Z0-9_]|$)").test(upper)) return tags[i];
        }
        if (upper.indexOf("UEFA WOMEN") !== -1) return "UWCL";
        if (/(^|[^A-Z0-9_])UEFA([^A-Z0-9_]|$)/.test(upper)) return "UCL";
        return "NONE";
    }

    // CSV RFC 4180: comillas, comas y saltos de línea dentro de celdas (las cabeceras de la hoja los tienen)
    function parseCSV(text) {
        var rows = [], row = [], field = "", inQuotes = false;
        text = String(text).replace(/^﻿/, "");
        for (var i = 0; i < text.length; i++) {
            var c = text.charAt(i);
            if (inQuotes) {
                if (c === '"') {
                    if (text.charAt(i + 1) === '"') { field += '"'; i++; }
                    else inQuotes = false;
                } else field += c;
            } else if (c === '"') {
                inQuotes = true;
            } else if (c === ",") {
                row.push(field); field = "";
            } else if (c === "\n" || c === "\r") {
                if (c === "\r" && text.charAt(i + 1) === "\n") i++;
                row.push(field); rows.push(row); row = []; field = "";
            } else field += c;
        }
        if (field !== "" || row.length > 0) { row.push(field); rows.push(row); }
        return rows;
    }

    function parseSheetRows(rows) {
        var headerIdx = -1, r, c;
        for (r = 0; r < rows.length && headerIdx < 0; r++) {
            for (c = 0; c < rows[r].length; c++) {
                if (normHeader(rows[r][c]) === NAME_COLUMN) { headerIdx = r; break; }
            }
        }
        if (headerIdx < 0) throw new Error("No se encontró la columna 'Club Name' en la hoja.");

        var nameCol = -1, colForKey = {}, hasCompetitionCol = false;
        for (c = 0; c < rows[headerIdx].length; c++) {
            var h = normHeader(rows[headerIdx][c]);
            if (h === NAME_COLUMN && nameCol < 0) nameCol = c;
            if (COLUMN_MAP.hasOwnProperty(h) && !colForKey.hasOwnProperty(COLUMN_MAP[h])) {
                colForKey[COLUMN_MAP[h]] = c;
                if (COLUMN_MAP[h] === "Competition") hasCompetitionCol = true;
            }
        }
        var missing = [];
        for (var m = 0; m < REQUIRED.length; m++) if (!colForKey.hasOwnProperty(REQUIRED[m])) missing.push(REQUIRED[m]);
        if (missing.length) throw new Error("Faltan columnas obligatorias en la hoja: " + missing.join(", "));

        var stadiums = {}, names = [], warnings = [];
        for (r = headerIdx + 1; r < rows.length; r++) {
            var row = rows[r], lineNo = r + 1;
            var name = cleanName(nameCol < row.length ? row[nameCol] : "");
            if (!name) continue;

            var anyNumber = false;
            for (c = 0; c < row.length; c++) if (toInt(row[c])) { anyNumber = true; break; }
            if (!anyNumber) continue; // fila plantilla/vacía (p.ej. 'NUEVO ESTADIO')

            var profile = makeDefaults();
            for (var key in colForKey) {
                if (!colForKey.hasOwnProperty(key)) continue;
                var raw = colForKey[key] < row.length ? row[colForKey[key]] : "";
                if (key === "Competition") {
                    if (cleanName(raw)) profile[key] = cleanName(raw).toUpperCase();
                    continue;
                }
                var val = toInt(raw);
                if (val !== null) profile[key] = val;
            }
            if (!hasCompetitionCol) profile["Competition"] = inferCompetition(name);

            var bad = [];
            for (m = 0; m < REQUIRED.length; m++) if (!profile[REQUIRED[m]]) bad.push(REQUIRED[m]);
            if (bad.length) { warnings.push(name + ": falta " + bad.join(", ") + " (fila " + lineNo + ")"); continue; }
            if (profile["Canvas Height"] > 4320 || profile["Canvas Width"] > 8192) {
                warnings.push(name + ": canvas no válido " + profile["Canvas Width"] + "x" + profile["Canvas Height"] + " (fila " + lineNo + ")");
                continue;
            }
            if (stadiums.hasOwnProperty(name)) {
                warnings.push(name + ": nombre duplicado, se usa la primera fila (fila " + lineNo + ")");
                continue;
            }
            stadiums[name] = profile;
            names.push(name);
        }
        return { stadiums: stadiums, names: names, warnings: warnings };
    }

    function isWindows() { return $.os.toLowerCase().indexOf("windows") !== -1; }

    function readTextFile(path) {
        var f = new File(path);
        if (!f.exists) return null;
        f.encoding = "UTF-8";
        if (!f.open("r")) return null;
        var txt = f.read();
        f.close();
        return txt;
    }

    // ExtendScript no tiene HTTPS: se descarga con curl (Win10+/macOS) y, en Windows, PowerShell como plan B.
    function scriptsCanUseNetwork() {
        try { return app.preferences.getPrefAsLong("Main Pref Section", "Pref_SCRIPTING_FILE_NETWORK_SECURITY") === 1; }
        catch (e) { return true; } // si no se puede consultar, se intenta igualmente
    }

    function downloadSheetCSV(progress) {
        if (!scriptsCanUseNetwork()) {
            throw new Error("Activa 'Permitir que los scripts escriban archivos y accedan a la red' en Preferencias > Scripting y expresiones.");
        }
        var tmpPath = Folder.temp.fsName + (isWindows() ? "\\" : "/") + "archie_stadiums_" + new Date().getTime() + ".csv";
        var commands = [];
        if (isWindows()) {
            commands.push('cmd.exe /c curl.exe -sSL --max-time ' + FETCH_TIMEOUT + ' -H "Cache-Control: no-cache" -o "' + tmpPath + '" "' + SHEET_CSV_URL + '"');
            commands.push('powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "' +
                "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; " +
                "Invoke-WebRequest -UseBasicParsing -TimeoutSec " + FETCH_TIMEOUT + " -Headers @{'Cache-Control'='no-cache'} " +
                "-Uri '" + SHEET_CSV_URL + "' -OutFile '" + tmpPath + "'" + '"');
        } else {
            commands.push("/usr/bin/curl -sSL --max-time " + FETCH_TIMEOUT + " -H 'Cache-Control: no-cache' -o '" + tmpPath + "' '" + SHEET_CSV_URL + "'");
        }

        var lastError = "sin respuesta";
        for (var i = 0; i < commands.length; i++) {
            try {
                if (progress && i > 0) progress("Reintentando descarga con PowerShell...", 45);
                var out = system.callSystem(commands[i]);
                var txt = readTextFile(tmpPath);
                try { new File(tmpPath).remove(); } catch (eRm) {}
                if (txt && strTrim(txt).length > 0) {
                    var head = txt.substring(0, 500).toLowerCase();
                    if (head.indexOf("<!doctype html") !== -1 || head.indexOf("<html") !== -1) {
                        // Google devuelve la página de login si la hoja no es accesible por enlace
                        throw new Error("La hoja no es accesible (¿compartida como 'Cualquiera con el enlace'?).");
                    }
                    return txt;
                }
                if (out) lastError = strTrim(out).substring(0, 200);
            } catch (e) {
                lastError = e.message || e.toString();
                if (lastError.indexOf("no es accesible") !== -1) throw e;
            }
        }
        throw new Error(lastError);
    }

    // ExtendScript (ES3) no trae JSON.stringify garantizado: serializador mínimo para la caché
    function jsonQuote(s) {
        return '"' + String(s).replace(/\\/g, "\\\\").replace(/"/g, '\\"')
            .replace(/\n/g, "\\n").replace(/\r/g, "\\r").replace(/\t/g, "\\t") + '"';
    }
    function toJSON(v, indent) {
        indent = indent || "";
        if (v === null || v === undefined) return "null";
        if (typeof v === "number") return isFinite(v) ? String(v) : "null";
        if (typeof v === "boolean") return v ? "true" : "false";
        if (typeof v === "string") return jsonQuote(v);
        var inner = indent + "    ", parts = [], k;
        if (v instanceof Array) {
            for (k = 0; k < v.length; k++) parts.push(inner + toJSON(v[k], inner));
            return parts.length ? "[\n" + parts.join(",\n") + "\n" + indent + "]" : "[]";
        }
        for (k in v) if (v.hasOwnProperty(k)) parts.push(inner + jsonQuote(k) + ": " + toJSON(v[k], inner));
        return parts.length ? "{\n" + parts.join(",\n") + "\n" + indent + "}" : "{}";
    }

    function writeCache(lib) {
        try {
            var folder = new Folder(CACHE_FOLDER_PATH);
            if (!folder.exists) folder.create();
            var obj = {};
            for (var i = 0; i < lib.names.length; i++) obj[lib.names[i]] = lib.data[lib.names[i]];
            var f = new File(CACHE_FILE_PATH);
            f.encoding = "UTF-8";
            if (!f.open("w")) return false;
            f.write(toJSON(obj));
            f.close();
            return true;
        } catch (e) { return false; }
    }

    // Normaliza a { data, names } con "Custom" siempre primero (mismo criterio que _with_custom)
    function buildLibrary(dataObj, orderedNames) {
        var data = { "Custom": makeCustomProfile() }, names = ["Custom"], k;
        var keys = orderedNames;
        if (!keys) { keys = []; for (k in dataObj) if (dataObj.hasOwnProperty(k)) keys.push(k); }
        for (var i = 0; i < keys.length; i++) {
            if (keys[i] === "Custom" || !dataObj.hasOwnProperty(keys[i])) continue;
            data[keys[i]] = dataObj[keys[i]];
            names.push(keys[i]);
        }
        return { data: data, names: names };
    }

    /*
        Devuelve { data, names, source, message, warnings }
        source ∈ "sheets" | "cache" | "json" | "default"
        previousNames (opcional): lista anterior para informar de estadios nuevos/eliminados en un Refresh.
    */
    function loadStadiumLibrary(previousNames, progress) {
        if (!progress) progress = function() {};
        var warnings = [], sheetError = "";
        progress("Leyendo copia local...", 5);
        var cached = parseJSONFile(new File(CACHE_FILE_PATH), true);

        try {
            progress("Descargando estadios desde Google Sheets...", 15);
            var csvText = downloadSheetCSV(progress);
            progress("Procesando presets de estadios...", 75);
            var parsed = parseSheetRows(parseCSV(csvText));
            if (parsed.names.length > 0) {
                var lib = buildLibrary(parsed.stadiums, parsed.names);
                lib.source = "sheets";
                lib.warnings = parsed.warnings;
                progress("Guardando copia local...", 90);
                var cacheOk = writeCache(lib);

                var prev = previousNames;
                if (!prev && cached) { prev = []; for (var c in cached) if (cached.hasOwnProperty(c)) prev.push(c); }
                var msg = parsed.names.length + " estadios desde Google Sheets.";
                if (prev) {
                    var prevSet = {}, newSet = {}, nuevos = [], quitados = [], i;
                    for (i = 0; i < prev.length; i++) prevSet[prev[i]] = true;
                    for (i = 0; i < parsed.names.length; i++) { newSet[parsed.names[i]] = true; if (!prevSet[parsed.names[i]]) nuevos.push(parsed.names[i]); }
                    for (i = 0; i < prev.length; i++) if (prev[i] !== "Custom" && !newSet[prev[i]]) quitados.push(prev[i]);
                    if (nuevos.length) msg += " Nuevos: " + nuevos.length + ".";
                    if (quitados.length) msg += " Eliminados: " + quitados.length + ".";
                    lib.added = nuevos;
                    lib.removed = quitados;
                }
                if (parsed.warnings.length) msg += " Omitidos: " + parsed.warnings.length + ".";
                if (!cacheOk) msg += " (No se pudo guardar la copia local.)";
                lib.message = msg;
                return lib;
            }
            sheetError = "la hoja no tiene filas válidas";
        } catch (e) {
            sheetError = e.message || e.toString();
        }

        progress("Sin conexión, usando respaldo...", 85);
        if (cached) {
            var libC = buildLibrary(cached, null);
            libC.source = "cache";
            libC.warnings = warnings;
            libC.message = "SIN CONEXIÓN: " + (libC.names.length - 1) + " estadios desde la copia local (última sincronización). Motivo: " + sheetError;
            return libC;
        }

        var legacy = parseJSONFile(new File(STADIUM_LIBRARY_PATH), true);
        if (legacy) {
            var libJ = buildLibrary(legacy, null);
            libJ.source = "json";
            libJ.warnings = warnings;
            libJ.message = "SIN CONEXIÓN: " + (libJ.names.length - 1) + " estadios desde stadium_library.json (respaldo). Motivo: " + sheetError;
            return libJ;
        }

        var libD = buildLibrary({}, []);
        libD.source = "default";
        libD.warnings = warnings;
        libD.message = "ERROR: no hay ninguna fuente de estadios; solo perfil Custom. Motivo: " + sheetError;
        return libD;
    }

    // =====================================================================
    // --- PREVISUALIZACIÓN 3D DESDE UN SYSTEM FILE ---
    // Con una comp seleccionada en el panel Proyecto se detectan las comps de zona que contiene
    // (Goal Line L/R, Touch Line L/R, Centerboard) y se monta una comp 3D con un campo de fútbol
    // vectorial y las zonas como pantallas LED en "U", reproduciendo su contenido real.
    // =====================================================================
    var ZONE_ORDER = ["ZONA_1", "ZONA_2", "ZONA_3", "ZONA_4", "ZONA_5"];
    var ZONE_NAMES = {
        "ZONA_1": "01 Goal Line Left", "ZONA_2": "02 Touch Line Left", "ZONA_3": "03 Centerboard",
        "ZONA_4": "04 Touch Line Right", "ZONA_5": "05 Goal Line Right"
    };

    // Comps únicas usadas dentro del system file (hasta 3 niveles de anidado), en orden de aparición
    function collectNestedComps(comp, depth, seen, out) {
        if (depth > 3) return out;
        for (var i = 1; i <= comp.numLayers; i++) {
            var src = comp.layer(i).source;
            if (!(src instanceof CompItem) || seen[src.id]) continue;
            seen[src.id] = true;
            out.push({ comp: src, depth: depth, layer: comp.layer(i) });
            collectNestedComps(src, depth + 1, seen, out);
        }
        return out;
    }

    function classifyZoneName(name) {
        var n = " " + String(name).toLowerCase().replace(/[_\-\.]+/g, " ") + " ";
        var isGoal = /goal ?line|\sgl\s|\sgoal\s/.test(n);
        var isTouch = /touch ?line|\stl\s|\stouch\s|\sside ?line/.test(n);
        var isCB = /cent(er|re) ?board|\scb\s/.test(n);
        var isLeft = /\sleft\s|\sl\s|\sizq/.test(n);
        var isRight = /\sright\s|\sr\s|\sder/.test(n);
        if (isCB && !isGoal && !isTouch) return "ZONA_3";
        if (isGoal && isLeft && !isRight) return "ZONA_1";
        if (isGoal && isRight && !isLeft) return "ZONA_5";
        if (isTouch && isLeft && !isRight) return "ZONA_2";
        if (isTouch && isRight && !isLeft) return "ZONA_4";
        var num = n.match(/^\s0?([1-5])\s/);
        if (num) return "ZONA_" + num[1];
        return null;
    }

    function detectSystemZones(sysComp) {
        var candidates = collectNestedComps(sysComp, 0, {}, []);
        var mapping = {}, found = 0, i;
        for (i = 0; i < candidates.length; i++) {
            var z = classifyZoneName(candidates[i].comp.name);
            if (z && !mapping[z]) { mapping[z] = candidates[i].comp; found++; }
        }
        if (found === 0) {
            // Sin nombres reconocibles: orden del tile stacking (fila y luego X) de las comps directas
            var direct = [];
            for (i = 0; i < candidates.length; i++) {
                if (candidates[i].depth !== 0) continue;
                var pos = candidates[i].layer.property("Position").value;
                direct.push({ comp: candidates[i].comp, y: pos[1], x: pos[0] });
            }
            direct.sort(function(a, b) { return (a.y - b.y) || (a.x - b.x); });
            var slots = direct.length >= 5 ? ZONE_ORDER
                      : direct.length === 4 ? ["ZONA_1", "ZONA_2", "ZONA_4", "ZONA_5"]
                      : direct.length === 3 ? ["ZONA_1", "ZONA_2", "ZONA_5"] : [];
            for (i = 0; i < slots.length; i++) mapping[slots[i]] = direct[i].comp;
        }
        return { mapping: mapping, candidates: candidates };
    }

    // Diálogo para revisar/corregir qué comp es cada zona
    function showZoneMappingDialog(sysComp, detection) {
        var win = new Window("dialog", "Archie // Zonas del System File");
        win.orientation = "column";
        win.alignChildren = "fill";
        win.margins = 18;
        win.spacing = 10;
        win.add("statictext", undefined, "System file: " + sysComp.name);
        var pnl = win.add("panel", undefined, "Comps detectadas (corrige si hace falta)");
        pnl.alignChildren = "fill";
        pnl.margins = 12;

        var options = ["— ninguna —"], optComps = [null], i;
        for (i = 0; i < detection.candidates.length; i++) {
            var c = detection.candidates[i];
            options.push(new Array(c.depth + 1).join("    ") + c.comp.name);
            optComps.push(c.comp);
        }

        var drops = {};
        for (var z = 0; z < ZONE_ORDER.length; z++) {
            var key = ZONE_ORDER[z];
            var row = pnl.add("group");
            row.orientation = "row";
            row.add("statictext", [0, 0, 140, 20], ZONE_NAMES[key] + ":");
            var dd = row.add("dropdownlist", undefined, options);
            dd.preferredSize.width = 300;
            dd.selection = 0;
            for (i = 1; i < optComps.length; i++) if (detection.mapping[key] === optComps[i]) { dd.selection = i; break; }
            drops[key] = dd;
        }

        var btns = win.add("group");
        btns.alignment = "right";
        var btnCancel = btns.add("button", undefined, "Cancelar", { name: "cancel" });
        var btnOk = btns.add("button", undefined, "Crear previsualización 3D", { name: "ok" });
        var result = null;
        btnCancel.onClick = function() { win.close(); };
        btnOk.onClick = function() {
            var m = {}, n = 0;
            for (var k in drops) {
                if (!drops.hasOwnProperty(k) || !drops[k].selection) continue;
                var comp = optComps[drops[k].selection.index];
                if (comp) { m[k] = comp; n++; }
            }
            if (n === 0) { alert("Asigna al menos una zona."); return; }
            result = m;
            win.close();
        };
        win.center();
        win.show();
        return result;
    }

    // --- Campo de fútbol vectorial (precomp 2D que luego se tumba en 3D) ---
    var PITCH_PX_PER_M = 20;

    function addShapeGroup(layer, name) {
        var grp = layer.property("ADBE Root Vectors Group").addProperty("ADBE Vector Group");
        grp.name = name;
        return grp.property("ADBE Vectors Group");
    }
    function addRectShape(contents, w, h, x, y) {
        var r = contents.addProperty("ADBE Vector Shape - Rect");
        r.property("ADBE Vector Rect Size").setValue([w, h]);
        r.property("ADBE Vector Rect Position").setValue([x, y]);
    }
    function addEllipseShape(contents, d, x, y) {
        var e = contents.addProperty("ADBE Vector Shape - Ellipse");
        e.property("ADBE Vector Ellipse Size").setValue([d, d]);
        e.property("ADBE Vector Ellipse Position").setValue([x, y]);
    }
    function addArcShape(contents, cx, cy, r, a0, a1) {
        var verts = [], n = 24;
        for (var i = 0; i <= n; i++) {
            var a = (a0 + (a1 - a0) * i / n) * Math.PI / 180;
            verts.push([cx + r * Math.cos(a), cy + r * Math.sin(a)]);
        }
        var shp = new Shape();
        shp.vertices = verts;
        shp.closed = false;
        contents.addProperty("ADBE Vector Shape - Group").property("ADBE Vector Shape").setValue(shp);
    }
    function addFill(contents, color) {
        contents.addProperty("ADBE Vector Graphic - Fill").property("ADBE Vector Fill Color").setValue(color);
    }
    function addStroke(contents, color, width) {
        var s = contents.addProperty("ADBE Vector Graphic - Stroke");
        s.property("ADBE Vector Stroke Color").setValue(color);
        s.property("ADBE Vector Stroke Width").setValue(width);
    }

    function buildPitchComp(folder, duration, fps) {
        var m = PITCH_PX_PER_M, margin = 6;
        var L = 105, W = 68;
        var cw = (L + 2 * margin) * m, ch = (W + 2 * margin) * m;
        var comp = app.project.items.addComp("PREVIEW_PITCH", cw, ch, 1.0, duration, fps);
        comp.parentFolder = folder;

        var layer = comp.layers.addShape();
        layer.name = "Pitch";
        layer.property("Position").setValue([cw / 2, ch / 2]);

        // Césped a franjas
        var grass = addShapeGroup(layer, "Grass");
        addRectShape(grass, cw, ch, 0, 0);
        addFill(grass, [0.110, 0.235, 0.165]);
        var stripes = addShapeGroup(layer, "Stripes");
        var stripeW = L * m / 14;
        for (var i = 0; i < 14; i += 2) addRectShape(stripes, stripeW, W * m, -L * m / 2 + stripeW * (i + 0.5), 0);
        addFill(stripes, [0.125, 0.260, 0.180]);

        // Líneas (coordenadas en metros desde el centro del campo)
        var lines = addShapeGroup(layer, "Lines");
        var hl = L / 2, hw = W / 2;
        addRectShape(lines, L * m, W * m, 0, 0);
        addRectShape(lines, 0.01, W * m, 0, 0);                                   // medio campo
        addEllipseShape(lines, 18.3 * m, 0, 0);                                   // círculo central
        for (var sd = -1; sd <= 1; sd += 2) {
            var gx = sd * hl;
            addRectShape(lines, 16.5 * m, 40.32 * m, (gx - sd * 16.5 / 2) * m, 0);     // área grande
            addRectShape(lines, 5.5 * m, 18.32 * m, (gx - sd * 5.5 / 2) * m, 0);       // área pequeña
            addRectShape(lines, 2 * m, 7.32 * m, (gx + sd * 1) * m, 0);                // portería
            var spotX = gx - sd * 11;
            if (sd < 0) addArcShape(lines, spotX * m, 0, 9.15 * m, -53.13, 53.13);     // medialuna
            else addArcShape(lines, spotX * m, 0, 9.15 * m, 126.87, 233.13);
            addArcShape(lines, gx * m, -hw * m, 1 * m, sd < 0 ? 0 : 90, sd < 0 ? 90 : 180);     // córners
            addArcShape(lines, gx * m, hw * m, 1 * m, sd < 0 ? 270 : 180, sd < 0 ? 360 : 270);
        }
        addStroke(lines, [0.92, 0.95, 0.93], 0.12 * m);

        var dots = addShapeGroup(layer, "Spots");
        addEllipseShape(dots, 0.6 * m, 0, 0);
        addEllipseShape(dots, 0.5 * m, -(hl - 11) * m, 0);
        addEllipseShape(dots, 0.5 * m, (hl - 11) * m, 0);
        addFill(dots, [0.92, 0.95, 0.93]);

        return comp;
    }

    /*
        Monta la comp 3D. Unidades del mundo: 100 px = 1 m. Origen = centro del campo (en el null raíz).
        Eje X a la derecha, Y hacia abajo (suelo en Y = 0, los LED crecen hacia Y negativo), Z hacia el fondo.
          ZONA_1 -> fondo izquierdo, de la esquina cercana a la lejana   (Y Rotation -90: X local -> +Z)
          ZONA_2/3/4 -> lateral largo del fondo, de izquierda a derecha  (sin rotación, mirando a cámara)
          ZONA_5 -> fondo derecho, de la esquina lejana a la cercana     (Y Rotation +90: X local -> -Z)
    */
    function buildStadiumPreview3D(sysComp, mapping) {
        var U = 100;                       // px por metro
        var LONG_LEN = 113 * U;            // lateral: 105 m de campo + 4 m por lado
        var BZ = (68 / 2 + 4) * U;         // distancia del centro al LED del fondo
        var duration = sysComp.duration, fps = sysComp.frameRate;

        // Escala única (contenido sin deformar): el lateral largo ocupa LONG_LEN
        var longW = 0;
        for (var a = 1; a <= 3; a++) if (mapping[ZONE_ORDER[a]]) longW += mapping[ZONE_ORDER[a]].width;
        var k;
        if (longW > 0) k = LONG_LEN / longW;
        else {
            var glW = (mapping["ZONA_1"] || mapping["ZONA_5"]).width;
            k = (68 + 4) * U / glW;
        }
        var BX = longW > 0 ? (longW * k) / 2 : LONG_LEN / 2;

        app.beginUndoGroup("Archie: Previsualización 3D de " + sysComp.name);

        var folder = app.project.items.addFolder(sysComp.name + "_PREVIEW_3D");
        folder.parentFolder = sysComp.parentFolder;

        var comp = app.project.items.addComp("STADIUM_PREVIEW_3D - " + sysComp.name, 1920, 1080, 1.0, duration, fps);
        comp.parentFolder = folder;
        comp.bgColor = [0.067, 0.075, 0.090];

        // Null raíz con controles de órbita
        var root = comp.layers.addNull(duration);
        root.name = "STADIUM_ROOT (controles)";
        root.threeDLayer = true;
        root.property("Anchor Point").setValue([0, 0, 0]);
        root.property("Position").setValue([960, 540, 0]);
        var fx = root.property("ADBE Effect Parade");
        var sOrbit = fx.addProperty("ADBE Slider Control");
        sOrbit.name = "Orbita";
        var cAuto = fx.addProperty("ADBE Checkbox Control");
        cAuto.name = "Auto Orbita";
        var sSpeed = fx.addProperty("ADBE Slider Control");
        sSpeed.name = "Velocidad (grados por seg)";
        sSpeed.property(1).setValue(6);
        root.property("ADBE Transform Group").property("ADBE Rotate Y").expression =
            'var s = effect("Orbita")(1);\n' +
            'effect("Auto Orbita")(1) == 1 ? s + time * effect("Velocidad (grados por seg)")(1) : s;';

        function addChild(item, name) {
            var l = comp.layers.add(item);
            l.name = name;
            l.threeDLayer = true;
            l.parent = root;
            return l;
        }

        // Campo tumbado en el suelo
        var pitch = buildPitchComp(folder, duration, fps);
        var pitchLayer = addChild(pitch, "PITCH");
        pitchLayer.collapseTransformation = true; // vectores nítidos aunque se escale
        pitchLayer.property("Anchor Point").setValue([pitch.width / 2, pitch.height / 2, 0]);
        pitchLayer.property("Position").setValue([0, 0, 0]);
        pitchLayer.property("Scale").setValue([U / PITCH_PX_PER_M * 100, U / PITCH_PX_PER_M * 100, 100]);
        pitchLayer.property("X Rotation").setValue(-90);
        pitchLayer.locked = true;

        // LEDs: ancla en la esquina inferior izquierda para que apoyen en el suelo
        function placeBoard(zoneKey, x, z, rotY) {
            var src = mapping[zoneKey];
            if (!src) return 0;
            var l = addChild(src, ZONE_NAMES[zoneKey]);
            l.property("Anchor Point").setValue([0, src.height, 0]);
            l.property("Scale").setValue([k * 100, k * 100, k * 100]);
            l.property("Y Rotation").setValue(rotY);
            l.property("Position").setValue([x, 0, z]);
            l.label = zoneKey === "ZONA_3" ? 8 : 9;
            if (rotY !== 0) ensureBoardDirection(l, src, z, rotY);
            return src.width * k;
        }

        // Comprobación del sentido de giro: el final del LED tiene que avanzar hacia donde toca
        // (ZONA_1 hacia el fondo, ZONA_5 hacia la cámara). Se mide con toWorld() vía una expresión temporal.
        function ensureBoardDirection(l, src, startZ, rotY) {
            try {
                var ctrl = l.property("ADBE Effect Parade").addProperty("ADBE Point3D Control");
                ctrl.property(1).expression = "toWorld([" + src.width + ", " + src.height + ", 0])";
                var endZ = ctrl.property(1).valueAtTime(0, false)[2];
                ctrl.remove();
                var wantsFar = rotY < 0; // ZONA_1 se coloca con -90 y debe ir hacia +Z
                if (wantsFar ? endZ < startZ : endZ > startZ) l.property("Y Rotation").setValue(-rotY);
            } catch (e) {}
        }

        var cursor = -BX;
        cursor += placeBoard("ZONA_2", cursor, BZ, 0);
        cursor += placeBoard("ZONA_3", cursor, BZ, 0);
        placeBoard("ZONA_4", cursor, BZ, 0);
        if (mapping["ZONA_1"]) placeBoard("ZONA_1", -BX, BZ - mapping["ZONA_1"].width * k, -90);
        placeBoard("ZONA_5", BX, BZ, 90);

        // Cámara elevada mirando al lateral largo
        var cam = comp.layers.addCamera("PREVIEW_CAM", [960, 540]);
        var dist = 22000, tilt = 30 * Math.PI / 180;
        var target = [960, 540, 4 * U];
        cam.property("Point of Interest").setValue(target);
        cam.property("Position").setValue([target[0], target[1] - dist * Math.sin(tilt), target[2] - dist * Math.cos(tilt)]);
        try { cam.property("Camera Options").property("Zoom").setValue(2666.7); } catch (eZ) {}

        // Rótulo
        try {
            var title = comp.layers.addText(sysComp.name);
            title.name = "TITLE";
            var td = title.property("Source Text").value;
            td.fontSize = 34;
            td.fillColor = [0.85, 0.88, 0.90];
            td.applyFill = true;
            td.justification = ParagraphJustification.LEFT_JUSTIFY;
            title.property("Source Text").setValue(td);
            title.property("Position").setValue([60, 80]);
        } catch (eT) {}

        root.moveToBeginning();
        app.endUndoGroup();
        comp.openInViewer();
        return comp;
    }

    function runSystemFilePreview(sysComp) {
        var detection = detectSystemZones(sysComp);
        if (detection.candidates.length === 0) {
            alert("'" + sysComp.name + "' no contiene comps de zona.\nSelecciona el SYSTEM FILE (la comp que contiene Goal Line, Touch Line y Centerboard).");
            return;
        }
        var mapping = showZoneMappingDialog(sysComp, detection);
        if (mapping) buildStadiumPreview3D(sysComp, mapping);
    }

    // --- MODO SYSTEM FILE: una comp seleccionada en el panel Proyecto ---
    var projSel = app.project.selection;
    if (projSel.length === 1 && projSel[0] instanceof CompItem) {
        if (confirm("¿Crear una previsualización 3D del estadio a partir de '" + projSel[0].name + "'?\n\n(No = continuar con el modo normal de Archie)")) {
            runSystemFilePreview(projSel[0]);
            return;
        }
    }

    // --- DETECCIÓN DE JSON EXISTENTE O MODO MANUAL ---
    var jsonItems = [];
    for (var i = 1; i <= app.project.numItems; i++) {
        var item = app.project.item(i);
        if (item instanceof FootageItem && item.file && item.name.toLowerCase().indexOf(".json") !== -1) {
            jsonItems.push(item);
        }
    }

    if (jsonItems.length === 1) {
        executeFromJSONItem(jsonItems[0], true);
    } else if (jsonItems.length > 1) {
        showJSONSelectionDialog(jsonItems);
    } else {
        showManualSetupDialog();
    }

    // --- DIÁLOGO BATCH PARA MÚLTIPLES JSON ---
    function showJSONSelectionDialog(items) {
        var win = new Window("dialog", "Archie // Batch Procesador JSON");
        win.orientation = "column";
        win.alignChildren = "fill";
        win.spacing = 15;
        win.margins = 20;

        win.add("statictext", undefined, "Se han detectado múltiples archivos JSON en el proyecto.\nSelecciona los que deseas procesar:", {multiline: true});
        
        var scrollGroup = win.add("panel", undefined, "Archivos Disponibles");
        scrollGroup.orientation = "column";
        scrollGroup.alignChildren = "left";
        scrollGroup.maximumSize.height = 300; 

        var checkboxes = [];
        for(var i = 0; i < items.length; i++) {
            var chk = scrollGroup.add("checkbox", undefined, decodeURIComponent(items[i].name));
            chk.value = true; // Activados por defecto
            checkboxes.push({ ui: chk, item: items[i] });
        }

        var btnGroup = win.add("group");
        btnGroup.orientation = "row";
        btnGroup.alignment = "center";

        var btnSelectAll = btnGroup.add("button", undefined, "Seleccionar Todos");
        var btnDeselectAll = btnGroup.add("button", undefined, "Deseleccionar");

        btnSelectAll.onClick = function() { for(var i=0; i<checkboxes.length; i++) checkboxes[i].ui.value = true; };
        btnDeselectAll.onClick = function() { for(var i=0; i<checkboxes.length; i++) checkboxes[i].ui.value = false; };

        var actionGroup = win.add("group");
        actionGroup.alignment = "right";
        var btnCancel = actionGroup.add("button", undefined, "Cancelar");
        var btnOk = actionGroup.add("button", undefined, "Procesar Seleccionados");

        btnCancel.onClick = function() { win.close(); };
        
        btnOk.onClick = function() {
            var selectedItems = [];
            for (var j = 0; j < checkboxes.length; j++) {
                if (checkboxes[j].ui.value) {
                    selectedItems.push(checkboxes[j].item);
                }
            }
            if (selectedItems.length === 0) {
                alert("Selecciona al menos un archivo JSON para continuar.");
                return;
            }
            win.close();
            
            // Ejecutar en bucle los JSONs seleccionados
            for (var k = 0; k < selectedItems.length; k++) {
                executeFromJSONItem(selectedItems[k]);
            }
            alert("¡Batch completado con éxito!\nSe han procesado " + selectedItems.length + " arquitecturas.");
        };

        win.center();
        win.show();
    }

    // --- DIÁLOGO DE CONFIGURACIÓN MANUAL ---
    function showManualSetupDialog() {
        var loader = showLoadingWindow("Cargando presets de estadios");
        var library;
        try { library = loadStadiumLibrary(null, loader.step); }
        finally { loader.close(); }
        var stadiumData = library.data;
        var stadiumKeys = library.names;

        var win = new Window("dialog", "Archie // Creador de Arquitectura Manual");
        win.orientation = "column";
        win.alignChildren = "fill";
        win.spacing = 12;
        win.margins = 18;

        var grpStadium = win.add("panel", undefined, "1. Perfil de Estadio");
        grpStadium.alignChildren = "fill";
        grpStadium.margins = 10;

        var rowStadium = grpStadium.add("group");
        rowStadium.orientation = "row";
        rowStadium.alignChildren = ["fill", "center"];
        var dropStadiums = rowStadium.add("dropdownlist", undefined, []);
        dropStadiums.preferredSize.width = 300;
        var btnRefresh = rowStadium.add("button", undefined, "Refresh");
        btnRefresh.helpTip = "Vuelve a leer los estadios desde Google Sheets";

        var txtStatus = grpStadium.add("statictext", undefined, "", { multiline: true });
        txtStatus.preferredSize = [380, 32];

        var txtSpec = grpStadium.add("statictext", undefined, "");
        txtSpec.preferredSize.width = 380;

        function describeSpec(spec) {
            if (!spec) return "";
            return "Perímetro " + spec["Width"] + "x" + spec["Height"] +
                   "  ·  GL " + (spec["Goal Line Size"] || 0) +
                   "  ·  CB " + (spec["Center Board Width"] || 0) +
                   "  ·  Canvas " + spec["Canvas Width"] + "x" + spec["Canvas Height"];
        }

        function populateStadiums(keepName) {
            dropStadiums.removeAll();
            var selIdx = 0;
            for (var i = 0; i < stadiumKeys.length; i++) {
                dropStadiums.add("item", stadiumKeys[i]);
                if (keepName && stadiumKeys[i] === keepName) selIdx = i;
            }
            dropStadiums.selection = selIdx;
            txtStatus.text = library.message;
        }

        dropStadiums.onChange = function() {
            txtSpec.text = dropStadiums.selection ? describeSpec(stadiumData[stadiumKeys[dropStadiums.selection.index]]) : "";
        };

        btnRefresh.onClick = function() {
            var current = dropStadiums.selection ? stadiumKeys[dropStadiums.selection.index] : null;
            btnRefresh.enabled = false;
            btnRefresh.text = "Cargando...";
            txtStatus.text = "Descargando estadios desde Google Sheets...";
            win.update();

            var refreshLoader = showLoadingWindow("Actualizando estadios");
            var fresh;
            try { fresh = loadStadiumLibrary(stadiumKeys, refreshLoader.step); }
            finally { refreshLoader.close(); }
            // Si falla la conexión, no se sustituye una lista buena por una peor
            if (fresh.source === "sheets" || library.source !== "sheets") {
                library = fresh;
                stadiumData = library.data;
                stadiumKeys = library.names;
                populateStadiums(current);
            } else {
                txtStatus.text = "Refresh fallido; se mantiene la lista actual. " + fresh.message;
            }

            btnRefresh.text = "Refresh";
            btnRefresh.enabled = true;

            var detail = [];
            if (fresh.added && fresh.added.length) detail.push("Nuevos:\n  - " + fresh.added.join("\n  - "));
            if (fresh.removed && fresh.removed.length) detail.push("Ya no están en la hoja:\n  - " + fresh.removed.join("\n  - "));
            if (fresh.warnings && fresh.warnings.length) detail.push("Omitidos por datos incompletos:\n  - " + fresh.warnings.join("\n  - "));
            if (detail.length) alert(detail.join("\n\n"), "Archie // Estadios actualizados");
        };

        populateStadiums(null);
        dropStadiums.onChange();

        var grpAssets = win.add("panel", undefined, "2. Recursos Gráficos");
        grpAssets.alignChildren = "fill";
        grpAssets.margins = 10;
        grpAssets.spacing = 8;

        // --- AUTOCOMPLETADO DE RECURSOS POR NOMBRE DE ARCHIVO ---
        // Al elegir un archivo se buscan los otros tipos en la misma carpeta. Solo se rellena un tipo
        // si hay exactamente un candidato; con varios hay que elegirlo a mano.
        var MEDIA_EXT = /\.(png|jpe?g|tiff?|psd|tga|exr|bmp|gif|mov|mp4|m4v|avi|mxf|webm|mkv)$/i;
        var ASSET_PATTERNS = {
            logo: /(^|[^a-z])logos?([^a-z]|$)/i,
            anim: /(^|[^a-z])anim(ation|zone)?s?([^a-z]|$)/i,
            bg:   /(^|[^a-z])(bg|background|backgrounds)([^a-z]|$)/i
        };
        var ASSET_LABELS = { logo: "LOGO", anim: "ANIM", bg: "BG" };
        var lastFolder = null;

        function fileDisplayName(f) {
            try { return decodeURI(f.name); } catch (e) { return f.name; }
        }

        // Devuelve el tipo (logo/anim/bg) si el nombre encaja con uno solo; null si ninguno o es ambiguo
        function detectAssetType(name) {
            var found = null;
            for (var t in ASSET_PATTERNS) {
                if (!ASSET_PATTERNS.hasOwnProperty(t) || !ASSET_PATTERNS[t].test(name)) continue;
                if (found) return null;
                found = t;
            }
            return found;
        }

        function addFilePickerRow(parent, labelText, assetType) {
            var row = parent.add("group");
            row.orientation = "row";
            row.alignChildren = ["left", "center"];
            row.add("statictext", [0,0,90,20], labelText);
            var txtPath = row.add("edittext", [0,0,220,20], "Sin seleccionar...");
            txtPath.enabled = false;
            var btnBrowse = row.add("button", [0,0,70,20], "Buscar");
            
            var picker = { type: assetType, file: null, manual: false };
            picker.getFile = function() { return picker.file; };
            picker.setFile = function(f, isManual) {
                picker.file = f;
                picker.manual = isManual;
                txtPath.text = fileDisplayName(f) + (isManual ? "" : "  (auto)");
                txtPath.helpTip = f.fsName;
            };

            btnBrowse.onClick = function() {
                var f = lastFolder ? new File(lastFolder.fsName + "/").openDlg("Seleccionar " + labelText)
                                   : File.openDialog("Seleccionar " + labelText);
                if (f) {
                    picker.setFile(f, true);
                    lastFolder = f.parent;
                    autoCompleteFrom(f);
                }
            };
            return picker;
        }

        var pickerLogo = addFilePickerRow(grpAssets, "LOGO:", "logo");
        var pickerAnim = addFilePickerRow(grpAssets, "ANIM ZONE:", "anim");
        var pickerBG   = addFilePickerRow(grpAssets, "BACKGROUND:", "bg");
        var pickers = [pickerLogo, pickerAnim, pickerBG];

        var txtAuto = grpAssets.add("statictext", undefined, "Al elegir un archivo se autocompletan los demás de la misma carpeta.", { multiline: true });
        txtAuto.preferredSize = [380, 32];

        function autoCompleteFrom(selectedFile) {
            var folder = selectedFile.parent;
            if (!folder || !folder.exists) return;

            var candidates = { logo: [], anim: [], bg: [] };
            var files = folder.getFiles(function(f) { return f instanceof File && MEDIA_EXT.test(fileDisplayName(f)); });
            for (var i = 0; i < files.length; i++) {
                var name = fileDisplayName(files[i]);
                if (name.charAt(0) === ".") continue; // ocultos / ._ de macOS
                var t = detectAssetType(name);
                if (t) candidates[t].push(files[i]);
            }

            var filled = [], ambiguous = [], missing = [];
            for (var p = 0; p < pickers.length; p++) {
                var pk = pickers[p];
                // No se pisa una elección manual ni el archivo recién elegido
                if (pk.manual) continue;
                var list = candidates[pk.type];
                if (list.length === 1) { pk.setFile(list[0], false); filled.push(ASSET_LABELS[pk.type]); }
                else if (list.length > 1) ambiguous.push(ASSET_LABELS[pk.type] + " (" + list.length + ")");
                else missing.push(ASSET_LABELS[pk.type]);
            }

            var msg = [];
            if (filled.length) msg.push("Autocompletado: " + filled.join(", ") + ".");
            if (ambiguous.length) msg.push("Varios archivos de " + ambiguous.join(", ") + ": selecciónalos manualmente.");
            if (missing.length) msg.push("No encontrado en la carpeta: " + missing.join(", ") + ".");
            if (msg.length) txtAuto.text = msg.join(" ");
        }

        var chkLogoUnder = win.add("checkbox", undefined, "Activar Modo LOGO UNDER");
        chkLogoUnder.value = false;

        var grpBtns = win.add("group");
        grpBtns.orientation = "row";
        grpBtns.alignment = "right";
        
        var btnCancel = grpBtns.add("button", undefined, "Cancelar");
        btnCancel.onClick = function() { win.close(); };

        var btnCreate = grpBtns.add("button", undefined, "Generar Arquitectura");
        btnCreate.onClick = function() {
            if (!dropStadiums.selection) { alert("Selecciona un estadio."); return; }
            var selectedKey = stadiumKeys[dropStadiums.selection.index];
            var stadiumSpec = stadiumData[selectedKey];

            var logoFile = pickerLogo.getFile();
            var animFile = pickerAnim.getFile();
            var bgFile   = pickerBG.getFile();

            if (!logoFile || !animFile || !bgFile) {
                alert("Por favor, selecciona los 3 archivos requeridos (LOGO, ANIM ZONE y BACKGROUND).");
                return;
            }

            win.close();
            processManualArchitecture(selectedKey, stadiumSpec, logoFile, animFile, bgFile, chkLogoUnder.value);
        };

        win.center();
        win.show();
    }

    // =====================================================================
    // --- PREVISUALIZADOR 3D DEL ESTADIO ---
    // Campo en perspectiva dibujado con ScriptUIGraphics. Las zonas LED forman una "U":
    //   ZONA_1 (Goal Line Left)  -> fondo izquierdo, de la esquina cercana a la lejana
    //   ZONA_2 + ZONA_3 + ZONA_4 -> lateral largo del fondo, de izquierda a derecha
    //   ZONA_5 (Goal Line Right) -> fondo derecho, de la esquina lejana a la cercana
    // Devuelve true si el usuario confirma la generación.
    // =====================================================================
    function showStadiumPreview(stadiumName, zonas, info) {
        info = info || {};
        var PITCH_L = 105, PITCH_W = 68;          // metros
        var HL = PITCH_L / 2, HW = PITCH_W / 2;
        var BOARD_GAP = 4, BOARD_H = 5;            // altura exagerada para que se lea
        var BX = HL + BOARD_GAP, BZ = HW + BOARD_GAP;
        var CANVAS_W = 920, CANVAS_H = 500;

        var C = {
            bg:       [0.067, 0.075, 0.090, 1],
            grassA:   [0.110, 0.235, 0.165, 1],
            grassB:   [0.125, 0.260, 0.180, 1],
            line:     [0.92, 0.95, 0.93, 0.85],
            housing:  [0.03, 0.03, 0.035, 1],
            ledBg:    [0.20, 0.23, 0.27, 1],
            anim:     [0.24, 0.86, 0.52, 1],
            logo:     [0.96, 0.77, 0.26, 1],
            cb:       [0.29, 0.55, 1.00, 1],
            divider:  [0.067, 0.075, 0.090, 1],
            text:     [0.85, 0.88, 0.90, 1],
            textDim:  [0.55, 0.60, 0.64, 1]
        };

        // Recorrido de cada lado (en el plano del suelo) y zonas que lo forman
        var sides = [
            { label: "01 GOAL LINE LEFT", keys: ["ZONA_1"], a: [-BX, -HW - 2], b: [-BX, BZ], out: [-1, 0] },
            { label: "", keys: ["ZONA_2", "ZONA_3", "ZONA_4"], a: [-BX, BZ], b: [BX, BZ], out: [0, 1] },
            { label: "05 GOAL LINE RIGHT", keys: ["ZONA_5"], a: [BX, BZ], b: [BX, -HW - 2], out: [1, 0] }
        ];

        // Segmentos normalizados (0..1) a lo largo de cada lado
        function buildSideSegments(side) {
            var total = 0, k, z, e;
            for (k = 0; k < side.keys.length; k++) { z = zonas[side.keys[k]]; if (z) total += Math.max(0, z.width || 0); }
            side.total = total;
            side.zoneSpans = [];
            side.segments = [];
            if (total <= 0) return;
            var acc = 0;
            for (k = 0; k < side.keys.length; k++) {
                z = zonas[side.keys[k]];
                if (!z || !(z.width > 0)) continue;
                side.zoneSpans.push({ key: side.keys[k], t0: acc / total, t1: (acc + z.width) / total });
                if (side.keys[k] === "ZONA_3") {
                    side.segments.push({ tipo: "center_board", t0: acc / total, t1: (acc + z.width) / total });
                }
                var elems = z.elementos || [];
                // Logos primero y anims encima (en LOGO UNDER se solapan)
                for (var pass = 0; pass < 2; pass++) {
                    for (e = 0; e < elems.length; e++) {
                        var isAnim = elems[e].tipo === "anim_zone";
                        if ((pass === 0) === isAnim) continue;
                        var rel = (elems[e].x_absoluta || 0) - (z.x_inicio || 0);
                        side.segments.push({
                            tipo: elems[e].tipo,
                            t0: (acc + Math.max(0, rel)) / total,
                            t1: (acc + Math.min(z.width, rel + (elems[e].width || 0))) / total
                        });
                    }
                }
                acc += z.width;
            }
        }
        for (var s = 0; s < sides.length; s++) buildSideSegments(sides[s]);

        // Estadísticas por zona
        function countZone(key) {
            var z = zonas[key], a = 0, l = 0;
            if (!z) return null;
            var el = z.elementos || [];
            for (var i = 0; i < el.length; i++) { if (el[i].tipo === "anim_zone") a++; else if (el[i].tipo === "static_logo") l++; }
            return { anim: a, logo: l, width: z.width || 0 };
        }

        // --- Cámara (orbita alrededor del campo) ---
        var view = { yaw: 0, tilt: 32 };
        var DEG = Math.PI / 180;

        function makeCamera() {
            var dist = 135, target = [0, 0, 6];
            var yaw = view.yaw * DEG, tilt = view.tilt * DEG;
            var pos = [
                target[0] + dist * Math.cos(tilt) * Math.sin(yaw),
                target[1] + dist * Math.sin(tilt),
                target[2] - dist * Math.cos(tilt) * Math.cos(yaw)
            ];
            var fwd = norm3([target[0] - pos[0], target[1] - pos[1], target[2] - pos[2]]);
            var right = norm3(cross3([0, 1, 0], fwd));
            var up = cross3(fwd, right);
            return { pos: pos, fwd: fwd, right: right, up: up, scale: 1, ox: 0, oy: 0 };
        }
        function norm3(v) { var l = Math.sqrt(v[0]*v[0] + v[1]*v[1] + v[2]*v[2]) || 1; return [v[0]/l, v[1]/l, v[2]/l]; }
        function cross3(a, b) { return [a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0]]; }
        function dot3(a, b) { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2]; }

        function projectRaw(cam, x, y, z) {
            var d = [x - cam.pos[0], y - cam.pos[1], z - cam.pos[2]];
            var zc = Math.max(0.1, dot3(d, cam.fwd));
            return [dot3(d, cam.right) / zc, -dot3(d, cam.up) / zc, zc];
        }

        // Ajusta escala y centrado para que todo el estadio quepa en el lienzo
        function fitCamera(cam, w, h) {
            var pts = [[-BX - 6, 0, -HW - 9], [BX + 6, 0, -HW - 9], [-BX, BOARD_H + 5, BZ], [BX, BOARD_H + 5, BZ], [-BX, 0, BZ], [BX, 0, BZ], [-BX, BOARD_H, -HW - 2], [BX, BOARD_H, -HW - 2]];
            var minX = 1e9, maxX = -1e9, minY = 1e9, maxY = -1e9;
            for (var i = 0; i < pts.length; i++) {
                var p = projectRaw(cam, pts[i][0], pts[i][1], pts[i][2]);
                if (p[0] < minX) minX = p[0]; if (p[0] > maxX) maxX = p[0];
                if (p[1] < minY) minY = p[1]; if (p[1] > maxY) maxY = p[1];
            }
            var margin = 40, top = 30, bottom = 70;
            cam.scale = Math.min((w - 2 * margin) / (maxX - minX), (h - top - bottom) / (maxY - minY));
            cam.ox = (w - (maxX - minX) * cam.scale) / 2 - minX * cam.scale;
            cam.oy = top + ((h - top - bottom) - (maxY - minY) * cam.scale) / 2 - minY * cam.scale;
        }

        function P(cam, x, y, z) {
            var p = projectRaw(cam, x, y, z);
            return [cam.ox + p[0] * cam.scale, cam.oy + p[1] * cam.scale, p[2]];
        }

        // --- Primitivas de dibujo ---
        function drawPoly(g, pts, brush, pen) {
            if (pts.length < 2) return;
            var i;
            if (brush) {
                g.newPath(); g.moveTo(pts[0][0], pts[0][1]);
                for (i = 1; i < pts.length; i++) g.lineTo(pts[i][0], pts[i][1]);
                g.closePath(); g.fillPath(brush);
            }
            if (pen) {
                g.newPath(); g.moveTo(pts[0][0], pts[0][1]);
                for (i = 1; i < pts.length; i++) g.lineTo(pts[i][0], pts[i][1]);
                g.strokePath(pen);
            }
        }

        function groundLine(g, cam, pen, pts) {
            var proj = [];
            for (var i = 0; i < pts.length; i++) proj.push(P(cam, pts[i][0], 0, pts[i][1]));
            drawPoly(g, proj, null, pen);
        }

        function groundRect(g, cam, pen, x0, z0, x1, z1) {
            groundLine(g, cam, pen, [[x0, z0], [x1, z0], [x1, z1], [x0, z1], [x0, z0]]);
        }

        function groundArc(g, cam, pen, cx, cz, r, a0, a1, filledBrush) {
            var pts = [], n = 48;
            for (var i = 0; i <= n; i++) {
                var a = (a0 + (a1 - a0) * i / n) * DEG;
                pts.push(P(cam, cx + r * Math.cos(a), 0, cz + r * Math.sin(a)));
            }
            drawPoly(g, pts, filledBrush || null, filledBrush ? null : pen);
        }

        function drawPitch(g, cam) {
            var stripes = 14, sw = PITCH_L / stripes;
            var bA = g.newBrush(g.BrushType.SOLID_COLOR, C.grassA);
            var bB = g.newBrush(g.BrushType.SOLID_COLOR, C.grassB);
            // Césped con margen alrededor de las líneas
            drawPoly(g, [P(cam, -HL - 3, 0, -HW - 3), P(cam, HL + 3, 0, -HW - 3), P(cam, HL + 3, 0, HW + 3), P(cam, -HL - 3, 0, HW + 3)], bA, null);
            for (var i = 0; i < stripes; i++) {
                if (i % 2) continue;
                var x0 = -HL + i * sw, x1 = x0 + sw;
                drawPoly(g, [P(cam, x0, 0, -HW), P(cam, x1, 0, -HW), P(cam, x1, 0, HW), P(cam, x0, 0, HW)], bB, null);
            }

            var pen = g.newPen(g.PenType.SOLID_COLOR, C.line, 1.5);
            var dotBrush = g.newBrush(g.BrushType.SOLID_COLOR, C.line);
            groundRect(g, cam, pen, -HL, -HW, HL, HW);                 // banda y línea de gol
            groundLine(g, cam, pen, [[0, -HW], [0, HW]]);              // medio campo
            groundArc(g, cam, pen, 0, 0, 9.15, 0, 360);                // círculo central
            groundArc(g, cam, pen, 0, 0, 0.4, 0, 360, dotBrush);       // punto central

            for (var side = -1; side <= 1; side += 2) {
                var gx = side * HL;
                groundRect(g, cam, pen, gx, -20.16, gx - side * 16.5, 20.16);   // área grande
                groundRect(g, cam, pen, gx, -9.16, gx - side * 5.5, 9.16);      // área pequeña
                groundRect(g, cam, pen, gx, -3.66, gx + side * 2, 3.66);        // portería
                var spotX = gx - side * 11;
                groundArc(g, cam, pen, spotX, 0, 0.35, 0, 360, dotBrush);       // punto de penalti
                // Medialuna: solo la parte fuera del área (±53.13° desde el punto de penalti)
                if (side < 0) groundArc(g, cam, pen, spotX, 0, 9.15, -53.13, 53.13);
                else groundArc(g, cam, pen, spotX, 0, 9.15, 126.87, 233.13);
                // Córners
                groundArc(g, cam, pen, gx, -HW, 1, side < 0 ? 0 : 90, side < 0 ? 90 : 180);
                groundArc(g, cam, pen, gx, HW, 1, side < 0 ? 270 : 180, side < 0 ? 360 : 270);
            }
        }

        function boardPoint(side, t) {
            return [side.a[0] + (side.b[0] - side.a[0]) * t, side.a[1] + (side.b[1] - side.a[1]) * t];
        }

        function boardQuad(cam, side, t0, t1, y0, y1) {
            var p0 = boardPoint(side, t0), p1 = boardPoint(side, t1);
            return [P(cam, p0[0], y0, p0[1]), P(cam, p1[0], y0, p1[1]), P(cam, p1[0], y1, p1[1]), P(cam, p0[0], y1, p0[1])];
        }

        function drawBoards(g, cam) {
            var brushes = {
                housing: g.newBrush(g.BrushType.SOLID_COLOR, C.housing),
                ledBg: g.newBrush(g.BrushType.SOLID_COLOR, C.ledBg),
                anim_zone: g.newBrush(g.BrushType.SOLID_COLOR, C.anim),
                static_logo: g.newBrush(g.BrushType.SOLID_COLOR, C.logo),
                center_board: g.newBrush(g.BrushType.SOLID_COLOR, C.cb)
            };
            var dividerPen = g.newPen(g.PenType.SOLID_COLOR, C.divider, 2);
            var textPen = g.newPen(g.PenType.SOLID_COLOR, C.textDim, 1);

            // Pintar primero el lado más lejano a la cámara
            var order = [];
            for (var i = 0; i < sides.length; i++) {
                var mid = boardPoint(sides[i], 0.5);
                order.push({ side: sides[i], depth: projectRaw(cam, mid[0], 0, mid[1])[2] });
            }
            order.sort(function(a, b) { return b.depth - a.depth; });

            for (var o = 0; o < order.length; o++) {
                var sd = order[o].side;
                if (sd.total <= 0) continue;
                drawPoly(g, boardQuad(cam, sd, -0.004, 1.004, -0.25, BOARD_H + 0.25), brushes.housing, null);
                drawPoly(g, boardQuad(cam, sd, 0, 1, 0, BOARD_H), brushes.ledBg, null);
                for (var k = 0; k < sd.segments.length; k++) {
                    var seg = sd.segments[k];
                    if (seg.t1 <= seg.t0 || !brushes[seg.tipo]) continue;
                    var inset = seg.tipo === "center_board" ? 0 : 0.35;
                    drawPoly(g, boardQuad(cam, sd, seg.t0, seg.t1, inset, BOARD_H - inset), brushes[seg.tipo], null);
                }
                // Separadores entre zonas del lateral largo
                for (var zs = 1; zs < sd.zoneSpans.length; zs++) {
                    var p = boardPoint(sd, sd.zoneSpans[zs].t0);
                    drawPoly(g, [P(cam, p[0], -0.25, p[1]), P(cam, p[0], BOARD_H + 0.25, p[1])], null, dividerPen);
                }
                // Etiquetas: lateral largo una por zona encima del LED; fondos junto a la esquina cercana
                if (sd.out[1]) {
                    for (var zl = 0; zl < sd.zoneSpans.length; zl++) {
                        var span = sd.zoneSpans[zl];
                        var mp = boardPoint(sd, (span.t0 + span.t1) / 2);
                        drawLabel(g, ZONE_LABELS[span.key], textPen, P(cam, mp[0], BOARD_H + 2.5, mp[1]));
                    }
                } else {
                    var near = boardPoint(sd, sd.a[1] < sd.b[1] ? 0 : 1);
                    drawLabel(g, sd.label, textPen, P(cam, near[0], 0, near[1] - 5));
                }
            }
        }

        var ZONE_LABELS = { "ZONA_2": "02 TOUCH LINE LEFT", "ZONA_3": "03 CB", "ZONA_4": "04 TOUCH LINE RIGHT" };

        function drawLabel(g, text, pen, anchor) {
            var tw = 0;
            try { tw = g.measureString(text)[0]; } catch (eM) { tw = text.length * 6; }
            g.drawString(text, pen, anchor[0] - tw / 2, anchor[1] - 8);
        }

        function drawLegend(g, w, h) {
            var items = [["ANIM ZONE", C.anim], ["LOGO", C.logo], ["CENTERBOARD", C.cb], ["BACKGROUND", C.ledBg]];
            var x = 24, y = h - 34;
            var pen = g.newPen(g.PenType.SOLID_COLOR, C.text, 1);
            for (var i = 0; i < items.length; i++) {
                drawPoly(g, [[x, y], [x + 14, y], [x + 14, y + 10], [x, y + 10]], g.newBrush(g.BrushType.SOLID_COLOR, items[i][1]), null);
                g.drawString(items[i][0], pen, x + 20, y - 2);
                var tw = 0;
                try { tw = g.measureString(items[i][0])[0]; } catch (eM) { tw = items[i][0].length * 7; }
                x += 20 + tw + 26;
            }
        }

        // --- Ventana ---
        var win = new Window("dialog", "Archie // Previsualización del estadio");
        win.orientation = "column";
        win.alignChildren = "fill";
        win.margins = 16;
        win.spacing = 10;

        var hdr = win.add("group");
        hdr.orientation = "row";
        hdr.alignChildren = ["left", "center"];
        var lblName = hdr.add("statictext", undefined, stadiumName);
        try { lblName.graphics.font = ScriptUI.newFont(lblName.graphics.font.name, "BOLD", 15); } catch (eF) {}
        if (info.subtitle) hdr.add("statictext", undefined, "  ·  " + info.subtitle);

        var canvas = win.add("group");
        canvas.preferredSize = [CANVAS_W, CANVAS_H];
        canvas.onDraw = function() {
            var g = this.graphics, w = this.size[0], h = this.size[1];
            drawPoly(g, [[0, 0], [w, 0], [w, h], [0, h]], g.newBrush(g.BrushType.SOLID_COLOR, C.bg), null);
            var cam = makeCamera();
            fitCamera(cam, w, h);
            drawPitch(g, cam);
            drawBoards(g, cam);
            drawLegend(g, w, h);
        };
        function redraw() { canvas.hide(); canvas.show(); }

        // Controles de vista
        var ctrl = win.add("group");
        ctrl.orientation = "row";
        ctrl.alignChildren = ["left", "center"];
        ctrl.add("statictext", undefined, "Órbita:");
        var sldYaw = ctrl.add("slider", undefined, view.yaw, -60, 60);
        sldYaw.preferredSize.width = 220;
        ctrl.add("statictext", undefined, "   Altura:");
        var sldTilt = ctrl.add("slider", undefined, view.tilt, 12, 70);
        sldTilt.preferredSize.width = 160;
        var btnReset = ctrl.add("button", undefined, "Vista frontal");
        sldYaw.onChanging = function() { view.yaw = sldYaw.value; redraw(); };
        sldTilt.onChanging = function() { view.tilt = sldTilt.value; redraw(); };
        btnReset.onClick = function() { view.yaw = 0; view.tilt = 32; sldYaw.value = 0; sldTilt.value = 32; redraw(); };

        // Resumen por zona
        var stats = win.add("panel", undefined, "Distribución");
        stats.orientation = "row";
        stats.alignChildren = ["fill", "top"];
        stats.margins = [12, 14, 12, 10];
        var statDefs = [["ZONA_1", "01 Goal Line Left"], ["ZONA_2", "02 Touch Line Left"], ["ZONA_3", "03 Centerboard"], ["ZONA_4", "04 Touch Line Right"], ["ZONA_5", "05 Goal Line Right"]];
        for (var sd = 0; sd < statDefs.length; sd++) {
            var cnt = countZone(statDefs[sd][0]);
            var col = stats.add("group");
            col.orientation = "column";
            col.alignChildren = "left";
            col.spacing = 2;
            col.preferredSize.width = 165;
            col.add("statictext", undefined, statDefs[sd][1]);
            col.add("statictext", undefined, cnt ? (cnt.width + " px") : "—");
            col.add("statictext", undefined, cnt ? (statDefs[sd][0] === "ZONA_3" ? "Centerboard" : (cnt.anim + " anim  ·  " + cnt.logo + " logo")) : "");
        }

        var btns = win.add("group");
        btns.orientation = "row";
        btns.alignment = "right";
        var btnCancel = btns.add("button", undefined, "Cancelar", { name: "cancel" });
        var btnOk = btns.add("button", undefined, info.okText || "Generar en After Effects", { name: "ok" });
        var confirmed = false;
        btnCancel.onClick = function() { win.close(); };
        btnOk.onClick = function() { confirmed = true; win.close(); };

        win.center();
        win.show();
        return confirmed;
    }

    // --- PROCESAMIENTO MANUAL ---
    function processManualArchitecture(stadiumName, spec, logoFile, animFile, bgFile, isLogoUnder) {
        app.beginUndoGroup("Generar Arquitectura Manual");
        var masterFolderName = stadiumName.toUpperCase().replace(/\s+/g, "_") + "_ARCHIE";
        
        function getOrCreateFolder(folderName, parentFolder) {
            var currentParent = parentFolder ? parentFolder : app.project.rootFolder;
            for (var i = 1; i <= currentParent.numItems; i++) {
                if (currentParent.item(i) instanceof FolderItem && currentParent.item(i).name === folderName) {
                    return currentParent.item(i);
                }
            }
            var newFolder = app.project.items.addFolder(folderName);
            newFolder.parentFolder = currentParent;
            return newFolder;
        }

        var fMaster = getOrCreateFolder(masterFolderName, app.project.rootFolder);
        var fOutput = getOrCreateFolder("00_Output", fMaster);
        var fInput = getOrCreateFolder("01_Input", fMaster);
        var fSystem = getOrCreateFolder("03_System comps", fMaster);
        var fDropZones = getOrCreateFolder("04_Drop Zones", fMaster);

        var logoFootage = app.project.importFile(new ImportOptions(logoFile));
        logoFootage.parentFolder = fInput;
        var animFootage = app.project.importFile(new ImportOptions(animFile));
        animFootage.parentFolder = fInput;
        var bgFootage = app.project.importFile(new ImportOptions(bgFile));
        bgFootage.parentFolder = fInput;

        var wTotal = spec["Width"] || 39456;
        var hTotal = spec["Height"] || 144;
        var glSize = spec["Goal Line Size"] || 11232;
        var cbWidth = spec["Center Board Width"] || 0;

        var masterDuration = animFootage.duration > 0 ? animFootage.duration : 10.0;
        // Archie v0.57: fps de la columna FPS de la hoja; si falta, el del vídeo de animación
        var masterFps = (spec["FPS"] > 0 && spec["FPS"] <= 120) ? spec["FPS"] : (animFootage.frameRate > 0 ? animFootage.frameRate : 25.0);

        var scaledLogoW = Math.round(logoFootage.width * (hTotal / logoFootage.height));
        var scaledAnimW = Math.round(animFootage.width * (hTotal / animFootage.height));
        var scaledBgW = Math.round(bgFootage.width * (hTotal / bgFootage.height));

        // Creación del SYSTEM COMP BG
        var maxPossibleN = Math.floor(30000 / scaledBgW);
        var N = Math.min(10, Math.max(1, maxPossibleN)); 
        var sysBgCompW = N * scaledBgW;

        var sysBgComp = app.project.items.addComp("SYSTEM COMP BG", sysBgCompW, hTotal, 1.0, masterDuration, masterFps);
        sysBgComp.parentFolder = fSystem;

        for (var i = 0; i < N; i++) {
            var bgItem = sysBgComp.layers.add(bgFootage);
            bgItem.property("Anchor Point").setValue([0, 0]);
            bgItem.property("Position").setValue([i * scaledBgW, 0]);
            var scaleXBg = (scaledBgW / bgFootage.width) * 100;
            var scaleYBg = (hTotal / bgFootage.height) * 100;
            bgItem.property("Scale").setValue([scaleXBg, scaleYBg]);
        }

        // Generadores Matemáticos
        function getGoalLinePattern(zoneWidth, logoUnderMode) {
            var baseL = logoUnderMode ? 3 : 1;
            var maxK = 0;
            for (var k = 100; k >= 0; k--) {
                var totalL = (2 * baseL) + 1 + (2 * Math.max(0, k - 1));
                if ((totalL * scaledLogoW + (2 * k) * scaledAnimW) <= zoneWidth) { maxK = k; break; }
            }
            var pattern = [];
            for (var l = 0; l < baseL; l++) pattern.push("static_logo");
            for (var m = 0; m < maxK; m++) { pattern.push("anim_zone"); if (m < maxK - 1) pattern.push("static_logo"); }
            pattern.push("static_logo");
            for (var n = 0; n < maxK; n++) { pattern.push("anim_zone"); if (n < maxK - 1) pattern.push("static_logo"); }
            for (var p = 0; p < baseL; p++) pattern.push("static_logo");
            return pattern;
        }

        function getTouchLinePattern(zoneWidth, logoUnderMode) {
            var baseL = logoUnderMode ? 3 : 1;
            var maxK = 0;
            for (var k = 100; k >= 0; k--) {
                var totalL = (2 * baseL) + Math.max(0, k - 1);
                if ((totalL * scaledLogoW + k * scaledAnimW) <= zoneWidth) { maxK = k; break; }
            }
            var pattern = [];
            for (var l = 0; l < baseL; l++) pattern.push("static_logo");
            for (var m = 0; m < maxK; m++) { pattern.push("anim_zone"); if (m < maxK - 1) pattern.push("static_logo"); }
            for (var p = 0; p < baseL; p++) pattern.push("static_logo");
            return pattern;
        }

        function repartirEquidistante(xStart, longitudTramo, tiposElementos) {
            if (!tiposElementos || tiposElementos.length === 0 || longitudTramo <= 0) return [];
            var anchoOcupado = 0;
            for (var i = 0; i < tiposElementos.length; i++) anchoOcupado += (tiposElementos[i] === "anim_zone") ? scaledAnimW : scaledLogoW;
            var sobrante = longitudTramo - anchoOcupado;
            if (sobrante < 0) return [];
            var X = sobrante / tiposElementos.length;
            var posActual = xStart + (X / 2.0);
            var resultado = [];
            for (var j = 0; j < tiposElementos.length; j++) {
                var wElem = (tiposElementos[j] === "anim_zone") ? scaledAnimW : scaledLogoW;
                resultado.push({ tipo: tiposElementos[j], width: wElem, x_absoluta: parseFloat(posActual.toFixed(4)) });
                posActual += wElem + X;
            }
            return resultado;
        }

        function generateUniformGrid(xStart, zWidth) {
            var maxLogos = Math.floor((zWidth + LOGO_UNDER_MARGIN) / (scaledLogoW + LOGO_UNDER_MARGIN));
            if (maxLogos <= 0) return [];
            var offsetX = xStart + (zWidth - (maxLogos * scaledLogoW + (maxLogos - 1) * LOGO_UNDER_MARGIN)) / 2.0;
            var grid = [];
            for (var i = 0; i < maxLogos; i++) {
                grid.push({ tipo: "static_logo", width: scaledLogoW, x_absoluta: parseFloat((offsetX + i * (scaledLogoW + LOGO_UNDER_MARGIN)).toFixed(4)), logo_under_type: "frozen" });
            }
            return grid;
        }

        function buildZoneData(xStart, zWidth, isGoalLine) {
            if (zWidth <= 0) return [];
            var pattern = isGoalLine ? getGoalLinePattern(zWidth, isLogoUnder) : getTouchLinePattern(zWidth, isLogoUnder);
            var geometricElems = repartirEquidistante(xStart, zWidth, pattern);

            if (!isLogoUnder) {
                for (var i = 0; i < geometricElems.length; i++) if (geometricElems[i].tipo === "static_logo") geometricElems[i].logo_under_type = "frozen";
                return geometricElems;
            } else {
                var animsOnly = [];
                for (var j = 0; j < geometricElems.length; j++) if (geometricElems[j].tipo === "anim_zone") animsOnly.push(geometricElems[j]);
                var baseGrid = generateUniformGrid(xStart, zWidth);

                for (var g = 0; g < baseGrid.length; g++) {
                    var logoX = baseGrid[g].x_absoluta;
                    var isOverlapping = false;
                    for (var a = 0; a < animsOnly.length; a++) {
                        if (logoX < animsOnly[a].x_absoluta + animsOnly[a].width - 1 && logoX + baseGrid[g].width > animsOnly[a].x_absoluta + 1) {
                            isOverlapping = true; break;
                        }
                    }
                    baseGrid[g].logo_under_type = isOverlapping ? "animated" : "frozen";
                }
                return baseGrid.concat(animsOnly);
            }
        }

        var cbStart = (wTotal / 2) - (cbWidth / 2);
        var cbEnd = cbStart + cbWidth;

        var z1W = glSize, z2Start = z1W, z2W = cbStart - z2Start;
        var z4Start = cbEnd, z4W = (wTotal - glSize) - z4Start;
        var z5Start = wTotal - glSize, z5W = glSize;

        var zonas = {
            "ZONA_1": { x_inicio: 0, width: z1W, elementos: buildZoneData(0, z1W, true) },
            "ZONA_2": { x_inicio: Math.floor(z2Start), width: Math.floor(z2W), elementos: buildZoneData(z2Start, z2W, false) },
            "ZONA_3": { x_inicio: Math.floor(cbStart), width: cbWidth, elementos: [] },
            "ZONA_4": { x_inicio: Math.floor(z4Start), width: Math.floor(z4W), elementos: buildZoneData(z4Start, z4W, false) },
            "ZONA_5": { x_inicio: Math.floor(z5Start), width: Math.floor(z5W), elementos: buildZoneData(z5Start, z5W, true) }
        };

        var subtitle = wTotal + " x " + hTotal + " px" + (isLogoUnder ? "  ·  LOGO UNDER" : "");
        if (!showStadiumPreview(stadiumName, zonas, { subtitle: subtitle })) {
            // Cancelado: se retira lo importado y las carpetas que hayan quedado vacías
            try { sysBgComp.remove(); } catch (eR1) {}
            try { logoFootage.remove(); } catch (eR2) {}
            try { animFootage.remove(); } catch (eR3) {}
            try { bgFootage.remove(); } catch (eR4) {}
            var created = [fOutput, fInput, fSystem, fDropZones, fMaster];
            for (var c = 0; c < created.length; c++) {
                try { if (created[c].numItems === 0) created[c].remove(); } catch (eR5) {}
            }
            app.endUndoGroup();
            return;
        }

        generateAEComps(stadiumName, masterFolderName, zonas, spec, logoFootage, animFootage, bgFootage, null, sysBgComp, sysBgCompW, hTotal, masterDuration, masterFps, fOutput, fDropZones, null);
        app.endUndoGroup();
    }

    // --- PROCESAMIENTO DESDE JSON ---
    function executeFromJSONItem(jsonItem, withPreview) {
        var data = parseJSONFile(jsonItem.file);
        if (!data) return false;

        var fileName = decodeURIComponent(jsonItem.name);
        var rawName = fileName.replace(/\.[^\.]+$/, ""); 

        var stadiumName = "ESTADIO";
        var stadiumMatch = fileName.match(/_ANIM_(?:\d+[a-z]+_)?(.*?)_ARCHIEtoAE/i);
        if (stadiumMatch && stadiumMatch[1]) {
            stadiumName = stadiumMatch[1].replace(/_/g, " ").replace(/\s+/g, " ").trim();
        }

        if (withPreview && data.zonas) {
            var dims = data.dimensiones_globales || {};
            var sub = (dims.width ? dims.width + " x " + (dims.height || 144) + " px  ·  " : "") + fileName;
            if (!showStadiumPreview(stadiumName, data.zonas, { subtitle: sub })) return false;
        }

        app.beginUndoGroup("Procesar JSON Archie: " + rawName);
        var masterFolderName = rawName; 

        function getOrCreateFolder(folderName, parentFolder) {
            var currentParent = parentFolder ? parentFolder : app.project.rootFolder;
            for (var i = 1; i <= currentParent.numItems; i++) {
                if (currentParent.item(i) instanceof FolderItem && currentParent.item(i).name === folderName) return currentParent.item(i);
            }
            var newFolder = app.project.items.addFolder(folderName);
            newFolder.parentFolder = currentParent;
            return newFolder;
        }

        var fMaster = getOrCreateFolder(masterFolderName, app.project.rootFolder);
        jsonItem.parentFolder = fMaster; 

        var fOutput = getOrCreateFolder("00_Output", fMaster);
        var fInput = getOrCreateFolder("01_Input", fMaster);
        var fSystem = getOrCreateFolder("03_System comps", fMaster);
        var fDropZones = getOrCreateFolder("04_Drop Zones", fMaster);

        function resolveAndImportAsset(rawPath, inputFolder) {
            if (!rawPath) return null;
            for (var i = 1; i <= inputFolder.numItems; i++) {
                var item = inputFolder.item(i);
                if (item instanceof FootageItem && item.file && item.file.fsName === new File(rawPath).fsName) return item;
            }
            var targetFile = new File(rawPath);
            if (!targetFile.exists) {
                var altPath = rawPath;
                if (rawPath.indexOf("192.168.110.242") !== -1) altPath = rawPath.replace("192.168.110.242", "192.168.20.242");
                else if (rawPath.indexOf("192.168.20.242") !== -1) altPath = rawPath.replace("192.168.20.242", "192.168.110.242");
                
                targetFile = new File(altPath);
                if (!targetFile.exists) {
                    targetFile = File.openDialog("ASSET OFFLINE: Selecciona manualmente el archivo para " + rawPath.substring(rawPath.lastIndexOf("/") + 1));
                    if (!targetFile || !targetFile.exists) return null;
                }
            }
            try {
                var importedFootage = app.project.importFile(new ImportOptions(targetFile));
                importedFootage.parentFolder = inputFolder;
                return importedFootage;
            } catch(e) { return null; }
        }

        var fuentes = data.archivos_fuente || {};
        var bgFootage = resolveAndImportAsset(fuentes.background, fInput);
        var logoFootage = resolveAndImportAsset(fuentes.static_logo, fInput);
        var animFootage = resolveAndImportAsset(fuentes.anim_zone, fInput);
        var cbFootage = resolveAndImportAsset(fuentes.center_board, fInput); // Soporte para Center Board desde JSON

        var hTotal = data.dimensiones_globales.height || 144;
        var masterDuration = (animFootage && animFootage.duration > 0) ? animFootage.duration : 10.0;
        // Archie v0.57: fps del JSON (columna FPS de la hoja); JSON antiguos -> el del vídeo de animación
        var masterFps = (data.fps > 0 && data.fps <= 120) ? data.fps : ((animFootage && animFootage.frameRate > 0) ? animFootage.frameRate : 25.0);

        var sysBgComp = null;
        var sysBgCompW = 0;
        if (bgFootage) {
            var scaledBgW = Math.round(bgFootage.width * (hTotal / bgFootage.height));
            var maxPossibleN = Math.floor(30000 / scaledBgW);
            var N = Math.min(10, Math.max(1, maxPossibleN)); 
            sysBgCompW = N * scaledBgW;

            sysBgComp = app.project.items.addComp("SYSTEM COMP BG", sysBgCompW, hTotal, 1.0, masterDuration, masterFps);
            sysBgComp.parentFolder = fSystem;

            for (var i = 0; i < N; i++) {
                var bgItem = sysBgComp.layers.add(bgFootage);
                bgItem.property("Anchor Point").setValue([0, 0]);
                bgItem.property("Position").setValue([i * scaledBgW, 0]);
                var scaleXBg = (scaledBgW / bgFootage.width) * 100;
                var scaleYBg = (hTotal / bgFootage.height) * 100;
                bgItem.property("Scale").setValue([scaleXBg, scaleYBg]);
            }
        }

        var fakeSpec = {
            "Canvas Width": data.system_file ? data.system_file.width : 3840,
            "Canvas Height": data.system_file ? data.system_file.height : 2160,
            "Canvas Mask": (data.system_file && data.system_file.mask) ? data.system_file.mask.width : 0,
            "Offset X": (data.system_file && data.system_file.offset) ? data.system_file.offset.x : 0,
            "Offset Y": (data.system_file && data.system_file.offset) ? data.system_file.offset.y : 0
        };

        // v20: edge case (Hoffenheim...): tramos explícitos del System File y zonas negras de brands
        var edge = {
            tramos: (data.system_file && data.system_file.tramos) ? data.system_file.tramos : null,
            negras: data.zonas_negras || null
        };
        generateAEComps(stadiumName, masterFolderName, data.zonas, fakeSpec, logoFootage, animFootage, bgFootage, cbFootage, sysBgComp, sysBgCompW, hTotal, masterDuration, masterFps, fOutput, fDropZones, edge);
        app.endUndoGroup();
    }

    // --- CREADOR DE COMPOSICIONES COMPARTIDO (Manual + JSON) ---
    function generateAEComps(stadiumName, masterFolderName, zonas, spec, logoFootage, animFootage, bgFootage, cbFootage, sysBgComp, sysBgCompW, hTotal, masterDuration, masterFps, fOutput, fDropZones, edge) {
        var tramos = (edge && edge.tramos && edge.tramos.length) ? edge.tramos : null;
        var negras = (edge && edge.negras && edge.negras.length) ? edge.negras : null;
        var zoneNames = {
            "ZONA_1": "01_Goal Line Left", "ZONA_2": "02_Touch Line Left",
            "ZONA_3": "03_Centerboard", "ZONA_4": "04_Touch Line Right", "ZONA_5": "05_Goal Line Right"
        };

        var createdZoneComps = {};
        var zoneKeys = ["ZONA_1", "ZONA_2", "ZONA_3", "ZONA_4", "ZONA_5"];

        // v22: en Logo Under los logos van DEBAJO de las ANIM ZONES
        var logosUnder = false;
        for (var zk in zonas) {
            var zEls = (zonas[zk] && zonas[zk].elementos) || [];
            for (var ze = 0; ze < zEls.length; ze++) {
                if (zEls[ze].tipo === "static_logo" && zEls[ze].logo_under_type === "animated") { logosUnder = true; break; }
            }
            if (logosUnder) break;
        }

        for (var z = 0; z < zoneKeys.length; z++) {
            var zKey = zoneKeys[z];
            var zData = zonas[zKey];
            if (!zData) continue; 
            var zWidth = zData.width;

            if (zWidth === 0 && zKey !== "ZONA_3") continue;

            var zComp = app.project.items.addComp(zoneNames[zKey], Math.max(1, zWidth), hTotal, 1.0, masterDuration, masterFps);
            zComp.parentFolder = fDropZones;
            createdZoneComps[zKey] = zComp;

            if (sysBgComp && bgFootage) {
                var bgLayer = zComp.layers.add(sysBgComp);
                bgLayer.name = "BACKGROUND";
                bgLayer.property("Anchor Point").setValue([0, 0]);
                bgLayer.property("Position").setValue([0, 0]);

                try {
                    var repetile = bgLayer.property("Effects").addProperty("CC Repetile");
                    try { repetile.property("Expand Right").setValue(Math.max(0, zWidth - sysBgCompW)); } 
                    catch(e1) { repetile.property(1).setValue(Math.max(0, zWidth - sysBgCompW)); }
                } catch(e) {
                    try {
                        var motionTile = bgLayer.property("Effects").addProperty("ADBE Tile");
                        motionTile.property("Output Width").setValue(Math.max(100, (zWidth / sysBgCompW) * 100));
                    } catch(err) {}
                }
                bgLayer.label = 1; 
                bgLayer.locked = true;
            }

            if (zKey === "ZONA_3" && zWidth > 0) {
                var cbSolid = zComp.layers.addSolid([0, 0, 0], "DROP_CenterBoard", zWidth, hTotal, 1.0, masterDuration);
                cbSolid.property("Anchor Point").setValue([0, 0]);
                cbSolid.property("Position").setValue([0, 0]);
                cbSolid.label = 1;
            }

            var elementos = zData.elementos || [];
            var animLayers = [];
            for (var e = 0; e < elementos.length; e++) {
                var elem = elementos[e];
                var footageToUse = null;

                // Asignación estricta por tipo de elemento
                if (elem.tipo === "anim_zone") {
                    footageToUse = animFootage;
                } else if (elem.tipo === "static_logo") {
                    footageToUse = logoFootage;
                } else if (elem.tipo === "center_board") {
                    footageToUse = cbFootage;
                }

                // Si no hay archivo cargado para este tipo de elemento, saltar iteración
                if (!footageToUse) continue;

                var layer = zComp.layers.add(footageToUse);
                layer.property("Anchor Point").setValue([0, 0]);
                var relX = elem.x_absoluta - (zData.x_inicio || 0);
                layer.property("Position").setValue([relX, 0]);

                var scaleX = (elem.width / footageToUse.width) * 100;
                var scaleY = (hTotal / footageToUse.height) * 100;
                layer.property("Scale").setValue([scaleX, scaleY]);

                // Etiquetas de Color
                if (elem.tipo === "anim_zone") {
                    animLayers.push(layer);
                    layer.label = 9; // Verde
                } else if (elem.tipo === "center_board") {
                    layer.label = 8; // Azul
                } else {
                    layer.label = 2; // Amarillo
                }

                if (elem.tipo === "static_logo" && (elem.logo_under_type === "frozen" || elem.freeze_on_frame_1 === true)) {
                    if (layer.canSetTimeRemapEnabled) {
                        layer.timeRemapEnabled = true;
                        var trProp = layer.property("ADBE Time Remapping");
                        while (trProp.numKeys > 1) { trProp.removeKey(2); }
                        if (trProp.numKeys === 1) {
                            trProp.setInterpolationTypeAtKey(1, KeyframeInterpolationType.HOLD, KeyframeInterpolationType.HOLD);
                        }
                    }
                }
            }

            // v22: Logo Under -> ANIM ZONES encima de todos los logos (se suben en orden, conservan su orden relativo)
            if (logosUnder) {
                for (var al = animLayers.length - 1; al >= 0; al--) animLayers[al].moveToBeginning();
            }

            // v20: zonas negras reservadas para brands (edge case), siempre encima de todo
            if (negras) {
                var zIni = zData.x_inicio || 0;
                for (var nz = 0; nz < negras.length; nz++) {
                    var nLo = Math.max(negras[nz][0], zIni), nHi = Math.min(negras[nz][1], zIni + zWidth);
                    if (nHi - nLo < 1) continue;
                    var nSolid = zComp.layers.addSolid([0, 0, 0], "RESERVED_BRAND", nHi - nLo, hTotal, 1.0, masterDuration);
                    nSolid.property("Anchor Point").setValue([0, 0]);
                    nSolid.property("Position").setValue([nLo - zIni, 0]);
                    nSolid.moveToBeginning();
                    nSolid.label = 0;
                    nSolid.locked = true;
                }
            }
        }

        // --- TILE STACKING OUTPUT ---
        var sysCanvasW = spec["Canvas Width"] || 3840;
        var sysCanvasH = spec["Canvas Height"] || 2160;
        var rightMargin = spec["Canvas Mask"] || 0;
        var offsetX = spec["Offset X"] || 0;
        var offsetY = spec["Offset Y"] || 0;
        
        var advance = sysCanvasW - rightMargin;
        if (advance <= 0) advance = sysCanvasW;

        var sysCompName = "OUTPUT_SYSTEM_CANVAS";
        var sysComp = app.project.items.addComp(sysCompName, sysCanvasW, sysCanvasH, 1.0, masterDuration, masterFps);
        sysComp.parentFolder = fOutput;

        var currentGlobalX = 0;

        // v20: tramo [x_ini_strip, x_fin_strip, x_lienzo, y_lienzo] -> una capa por zona que lo toca, recortada con máscara
        function addMaskRect(layer, x1, x2, h) {
            var m = layer.property("ADBE Mask Parade").addProperty("ADBE Mask Atom");
            var sh = new Shape();
            sh.vertices = [[x1, 0], [x2, 0], [x2, h], [x1, h]];
            sh.closed = true;
            m.property("ADBE Mask Shape").setValue(sh);
        }

        for (var zk = 0; zk < zoneKeys.length; zk++) {
            var kName = zoneKeys[zk];
            var tComp = createdZoneComps[kName];
            // v20: un Center Board de 0 px crea una comp de 1 px que no debe desplazar las zonas siguientes
            if (tComp && zonas[kName] && !(zonas[kName].width > 0)) tComp = null;
            if (tComp && zonas[kName] && typeof zonas[kName].x_inicio === "number") currentGlobalX = zonas[kName].x_inicio;
            if (tComp && tramos) {
                var tzW = zonas[kName].width;
                for (var ti = 0; ti < tramos.length; ti++) {
                    var s1 = tramos[ti][0], s2 = tramos[ti][1], tcx = tramos[ti][2], tcy = tramos[ti][3];
                    var lo = Math.max(s1, currentGlobalX), hi = Math.min(s2, currentGlobalX + tzW);
                    if (hi - lo < 1) continue;
                    var tLayer = sysComp.layers.add(tComp);
                    tLayer.property("Anchor Point").setValue([0, 0]);
                    tLayer.property("Position").setValue([tcx + currentGlobalX - s1, tcy]);
                    addMaskRect(tLayer, lo - currentGlobalX, hi - currentGlobalX, hTotal);
                }
                continue;
            }
            if (tComp) {
                var zW = tComp.width;
                
                var rowStart = Math.floor((currentGlobalX + offsetX) / advance);
                var rowEnd = Math.floor((currentGlobalX + zW - 1 + offsetX) / advance);

                var firstLayer = sysComp.layers.add(tComp);
                firstLayer.property("Anchor Point").setValue([0, 0]);
                firstLayer.property("Position").setValue([currentGlobalX - (rowStart * advance) + offsetX, (rowStart * hTotal) + offsetY]);
                
                for (var r = rowStart + 1; r <= rowEnd; r++) {
                    var copy = firstLayer.duplicate();
                    copy.property("Position").setValue([currentGlobalX - (r * advance) + offsetX, (r * hTotal) + offsetY]);
                }
                
                currentGlobalX += zW;
            }
        }

        var bgSolid = sysComp.layers.addSolid([0, 0, 0], "Background", sysCanvasW, sysCanvasH, 1.0, masterDuration);
        bgSolid.moveToEnd();
        bgSolid.locked = true;

        if (rightMargin > 0) {
            var mskSolid = sysComp.layers.addSolid([0, 0, 0], "Right Margin Mask", rightMargin, sysCanvasH, 1.0, masterDuration);
            mskSolid.property("Position").setValue([sysCanvasW - (rightMargin / 2), sysCanvasH / 2]);
            mskSolid.moveToBeginning();
            mskSolid.locked = true;
        }

        sysComp.openInViewer();
    }

})();