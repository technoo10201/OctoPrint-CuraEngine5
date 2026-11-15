/*
 * OctoPrint-CuraEngine5 — model of a profile being edited (shared by the settings page and the Slicer tab).
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
(function () {
    // A profile being edited: overrides (key -> text), setting rows, live recalculation.
window.CuraEngine5Profile = function (ctx, name, displayName, material, quality, overrides) {
        var prof = this;
        prof.name = name;
        prof.display_name = ko.observable(displayName);
        prof.material = ko.observable(material || "");
        prof.quality_type = ko.observable(quality || "");
        prof.overrides = $.extend({}, overrides || {});
        prof.mode = ko.observable("recommended");
        prof.level = ko.observable("basic");
        prof.search = ko.observable("");
        prof.categories = ko.observableArray([]);
        prof.category = ko.observable("");
        prof.rows = ko.observableArray([]);
        prof.rowsError = ko.observable("");
        prof.modifiedCount = ko.observable(_.size(prof.overrides));
        prof.dirty = ko.observable(false);   // modified since opening, not yet saved
        var seq = 0, timer = null;

        var body = function (extra) {
            return $.extend({material: prof.material(), quality_type: prof.quality_type(), overrides: prof.overrides}, extra);
        };
        var makeRow = function (r) {
            var row = {
                key: r.key, label: r.label, description: r.description, type: r.type, unit: r.unit,
                options: r.options, depth: r.depth, editable: r.editable,
                value: ko.observable(r.value), enabled: ko.observable(r.enabled),
                error: ko.observable(r.error || ""), warning: ko.observable(r.warning || ""),
                modified: ko.observable(prof.overrides.hasOwnProperty(r.key))
            };
            row.silent = false;
            row.value.subscribe(function (v) {
                if (row.silent) return;
                prof.overrides[row.key] = String(v);
                row.modified(true);
                prof.modifiedCount(_.size(prof.overrides));
                prof.dirty(true);
                prof.schedule();
            });
            row.reset = function () {
                delete prof.overrides[row.key];
                row.modified(false);
                prof.modifiedCount(_.size(prof.overrides));
                prof.dirty(true);
                prof.refresh();
            };
            return row;
        };
        var apply = function (values) {
            _.each(prof.rows(), function (row) {
                var v = values[row.key];
                if (!v) return;
                row.enabled(v.enabled);
                row.error(v.error || "");
                row.warning(v.warning || "");
                if (!row.modified()) {
                    row.silent = true;
                    row.value(v.value);
                    row.silent = false;
                }
            });
        };
        // light recalculation: only values, states and warnings of the displayed rows
        prof.refresh = function () {
            var keys = _.pluck(prof.rows(), "key");
            if (!keys.length) return;
            var n = ++seq;
            OctoPrint.postJson(ctx.uiUrl("values"), body({keys: keys})).done(function (r) {
                if (n === seq) apply(r.values || {});
            });
        };
        prof.schedule = function () {
            clearTimeout(timer);
            timer = setTimeout(prof.refresh, 350);
        };
        prof.loadRows = function () {
            var extra = {level: prof.level()};
            if (prof.mode() === "recommended") extra.recommended = true;
            else if (prof.search().trim()) extra.search = prof.search().trim();
            else if (prof.category()) extra.category = prof.category();
            else { prof.rows([]); return; }
            var n = ++seq;
            prof.rowsError("");
            OctoPrint.postJson(ctx.uiUrl("rows"), body(extra)).done(function (r) {
                if (n === seq) prof.rows(_.map(r.rows || [], makeRow));
            }).fail(function (xhr) { prof.rowsError(xhr.responseText || xhr.statusText); });
        };
        prof.loadCategories = function () {
            OctoPrint.postJson(ctx.uiUrl("categories"), {level: prof.level()}).done(function (r) {
                prof.categories(r.categories || []);
                var keys = _.pluck(r.categories, "key");
                if (!_.contains(keys, prof.category())) prof.category(keys[0] || "");
                else prof.loadRows();
            });
        };
        prof.setMode = function (mode) {
            prof.mode(mode);
            if (mode === "custom") prof.loadCategories(); else prof.loadRows();
        };
        prof.category.subscribe(function () { prof.loadRows(); });
        prof.level.subscribe(function () { prof.loadCategories(); });
        var searchTimer = null;
        prof.search.subscribe(function () {
            clearTimeout(searchTimer);
            searchTimer = setTimeout(prof.loadRows, 300);
        });
        [prof.display_name, prof.material, prof.quality_type].forEach(function (o) {
            o.subscribe(function () { prof.dirty(true); });
        });
        var onChoice = function () { ctx.onChoice(prof); prof.loadRows(); };
        prof.material.subscribe(onChoice);
        prof.quality_type.subscribe(onChoice);
    };


})();

/*
 * Machine editor (Cura's "Machine Settings") and add-printer wizard, shared
 * by the settings page and the Slicer tab.
 */
(function () {
    var FLAVORS = ["RepRap (Marlin/Sprinter)", "RepRap (Volumetric)", "RepRap (RepRap)", "Repetier", "UltiGCode",
                   "Griffin", "Makerbot", "BFB", "MACH3"];

    // Printer editing model, built from the GET /printers/<id> response
    window.CuraEngine5PrinterModel = function (p) {
        var g = p.settings.global, e = p.settings.extruder;
        var val = function (k) { var v = g[k].value; return v === null || v === undefined ? "" : v; };
        var m = this;
        m.id = p.id;
        m.name = ko.observable(p.name);
        m.isDefault = ko.observable(!!p["default"]);
        m.width = ko.observable(val("machine_width"));
        m.depth = ko.observable(val("machine_depth"));
        m.height = ko.observable(val("machine_height"));
        m.shape = ko.observable(val("machine_shape") || "rectangular");
        m.centerIsZero = ko.observable(!!g.machine_center_is_zero.value);
        m.heatedBed = ko.observable(!!g.machine_heated_bed.value);
        m.flavor = ko.observable(val("machine_gcode_flavor") || FLAVORS[0]);
        m.flavors = FLAVORS.indexOf(m.flavor()) < 0 ? [m.flavor()].concat(FLAVORS) : FLAVORS;
        m.nozzle = ko.observable(e.machine_nozzle_size.value);
        m.filament = ko.observable(e.material_diameter.value);
        m.startGcode = ko.observable(val("machine_start_gcode"));
        m.endGcode = ko.observable(val("machine_end_gcode"));
        var defaults = {start: g.machine_start_gcode["default"] || "", end: g.machine_end_gcode["default"] || ""};
        m.resetGcode = function () { m.startGcode(defaults.start); m.endGcode(defaults.end); };
        m.snapshot = "";
        m.isDirty = function () { return JSON.stringify(m.body()) !== m.snapshot; };
        m.body = function () {
            return {
                name: m.name(), "default": m.isDefault(),
                global: {machine_width: m.width(), machine_depth: m.depth(), machine_height: m.height(),
                         machine_shape: m.shape(), machine_center_is_zero: m.centerIsZero(),
                         machine_heated_bed: m.heatedBed(), machine_gcode_flavor: m.flavor(),
                         machine_start_gcode: m.startGcode(), machine_end_gcode: m.endGcode()},
                extruder: {machine_nozzle_size: m.nozzle(), material_diameter: m.filament()}
            };
        };
    };

    // (the snapshot is taken after construction, see below)
    var PrinterModel = window.CuraEngine5PrinterModel;
    window.CuraEngine5PrinterModel = function (p) {
        PrinterModel.call(this, p);
        this.snapshot = JSON.stringify(this.body());
    };

    // "Add a printer" wizard: Cura catalog grouped by manufacturer, search, creation.
    window.CuraEngine5Wizard = function (api, onCreated, onError) {
        var w = this, catalog = [], tick = ko.observable(0);
        w.visible = ko.observable(false);
        w.search = ko.observable("");
        w.pickedId = ko.observable("");
        w.newName = ko.observable("");
        w.busy = ko.observable(false);
        var norm = function (t) { return String(t).toLowerCase().replace(/[^a-z0-9]+/g, ""); };
        w.groups = ko.computed(function () {
            tick();
            var q = norm(w.search()), map = {}, order = [];
            _.each(catalog, function (m) {
                if (q && norm(m.manufacturer + m.name).indexOf(q) < 0) return;
                if (!map[m.manufacturer]) { map[m.manufacturer] = []; order.push(m.manufacturer); }
                map[m.manufacturer].push(m);
            });
            return _.map(order, function (name) { return {manufacturer: name, machines: map[name]}; });
        });
        w.open = function () {
            w.visible(true);
            w.pickedId("");
            w.search("");
            if (!catalog.length) {
                OctoPrint.get(api("catalog")).done(function (r) {
                    catalog = r.machines || [];
                    tick(tick() + 1);
                    if (r.error && onError) onError(r.error);
                });
            }
        };
        w.close = function () { w.visible(false); w.pickedId(""); };
        w.pick = function (m) { w.pickedId(m.id); w.newName(m.name); };
        w.create = function () {
            w.busy(true);
            OctoPrint.postJson(api("printers"), {definition: w.pickedId(), name: w.newName()})
                .done(function (p) { w.close(); onCreated(p); })
                .fail(function (xhr) { if (onError) onError((xhr && (xhr.responseText || xhr.statusText)) || "Error"); })
                .always(function () { w.busy(false); });
        };
    };
})();
