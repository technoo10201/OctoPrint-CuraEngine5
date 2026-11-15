/*
 * OctoPrint-CuraEngine5 — settings page
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
$(function () {
    function CuraEngine5ViewModel(parameters) {
        var self = this;
        self.settingsViewModel = parameters[0];
        self.settings = undefined;

        self.status = ko.observable({});
        self.printers = ko.observableArray([]);
        self.current = ko.observable(undefined);
        self.machine = ko.observable(undefined);
        self.printerError = ko.observable("");
        self.printerMessage = ko.observable("");
        self.busy = ko.observable(false);
        self.catalog = [];
        self.wizard = ko.observable(false);
        self.search = ko.observable("");
        self.pickedId = ko.observable("");
        self.newName = ko.observable("");
        self.profiles = ko.observableArray([]);
        self.profile = ko.observable(undefined);
        self.materials = ko.observableArray([]);
        self.qualityTypes = ko.observableArray([]);
        self.profileWarnings = ko.observableArray([]);
        var gcodeDefaults = {};

        self.platform = ko.observable({});
        self.installing = ko.observable(false);
        self.installMessage = ko.observable("");
        self.installOk = ko.observable(false);

        self.importDisplayName = ko.observable("");
        self.importDefault = ko.observable(false);
        self.importOverwrite = ko.observable(false);
        self.importing = ko.observable(false);
        self.importMessage = ko.observable("");
        self.importOk = ko.observable(false);

        var api = function (path) {
            return OctoPrint.getBlueprintUrl("curaengine5") + path;
        };

        self.refreshStatus = function () {
            OctoPrint.get(api("status")).done(self.status);
            OctoPrint.get(api("engine")).done(self.platform);
        };

        self.installEngine = function () {
            self.installing(true);
            self.installMessage("");
            OctoPrint.post(api("engine/install"), {})
                .done(function (r) {
                    self.installOk(true);
                    self.installMessage("CuraEngine " + (r.engine_version || "?") + " installed in " + r.engine_path + ".");
                    self.refreshStatus();
                })
                .fail(function (xhr) {
                    self.installOk(false);
                    self.installMessage("Installation failed: " + (xhr.responseText || xhr.statusText));
                })
                .always(function () {
                    self.installing(false);
                });
        };

        var fail = function (xhr) {
            self.printerError((xhr && (xhr.responseText || xhr.statusText)) || "Error");
        };
        var guarded = function (request) {
            self.busy(true);
            self.printerError("");
            return request.fail(fail).always(function () { self.busy(false); });
        };
        var bool = function (v) { return String(v).toLowerCase() === "true"; };

        self.loadPrinters = function () {
            return OctoPrint.get(api("printers")).done(function (r) { self.printers(r.printers || []); });
        };

        // --- "Add a printer" wizard (shared with the Slicer tab)
        self.wizard = new window.CuraEngine5Wizard(api, function (p) { self.loadPrinters(); showPrinter(p); },
                                                    function (msg) { self.printerError(msg); });

        // --- editing a printer
        var showPrinter = function (p) {
            self.printerMessage("");
            self.profile(undefined);
            self.machine(new window.CuraEngine5PrinterModel(p));
            self.current({id: ko.observable(p.id), name: ko.observable(p.name), material: p.material});
            self.loadProfiles();
        };
        self.editPrinter = function (p) {
            guarded(OctoPrint.get(api("printers/" + encodeURIComponent(p.id)))).done(showPrinter);
        };
        self.savePrinter = function () {
            var c = self.current();
            guarded(OctoPrint.putJson(api("printers/" + encodeURIComponent(c.id())), self.machine().body())).done(function (p) {
                self.loadPrinters();
                showPrinter(p);
                self.printerMessage("Saved. The OctoPrint printer profile is up to date.");
            });
        };
        self.deletePrinter = function (p) {
            if (!confirm("Delete printer \"" + p.name + "\", its print profiles and its OctoPrint profile?")) return;
            guarded(OctoPrint.delete(api("printers/" + encodeURIComponent(p.id)))).done(function () {
                if (self.current() && self.current().id() === p.id) { self.current(undefined); self.machine(undefined); self.profile(undefined); }
                self.loadPrinters();
            });
        };

        // --- print profiles
        self.loadProfiles = function () {
            var c = self.current();
            if (!c) return;
            OctoPrint.get(api("printers/" + encodeURIComponent(c.id()) + "/profiles")).done(function (r) {
                self.profiles(r.profiles || []);
            });
        };
        var loadOptions = function (material, quality, done) {
            var qs = "?material=" + encodeURIComponent(material || "") + "&quality_type=" + encodeURIComponent(quality || "");
            OctoPrint.get(api("printers/" + encodeURIComponent(self.current().id()) + "/options" + qs)).done(function (o) {
                self.materials(o.materials || []);
                self.qualityTypes(o.quality_types || []);
                self.profileWarnings(o.warnings || []);
                if (done) done(o);
            });
        };
        var uiUrl = function (what) {
            return api("printers/" + encodeURIComponent(self.current().id()) + "/ui/" + what);
        };

        var openProfile = function (name, displayName, material, quality, user) {
            loadOptions(material, quality, function (o) {
                var prof = new window.CuraEngine5Profile({
                    uiUrl: uiUrl, onChoice: function (p) { loadOptions(p.material(), p.quality_type()); }
                }, name, displayName, material, quality || o.quality_type, user);
                self.profile(prof);
                prof.loadRows();
            });
        };
        self.newProfile = function () {
            openProfile(null, "New profile", self.current().material, "", {});
        };
        self.editProfile = function (p) {
            openProfile(p.name, p.display_name, p.material, p.quality_type, p.user);
        };
        self.cancelProfile = function () { self.profile(undefined); };
        self.saveProfile = function () {
            var prof = self.profile();
            var body = {display_name: prof.display_name(), material: prof.material(), quality_type: prof.quality_type(),
                        user: prof.overrides};
            var base = api("printers/" + encodeURIComponent(self.current().id()) + "/profiles");
            var req = prof.name ? OctoPrint.putJson(base + "/" + encodeURIComponent(prof.name), body)
                                : OctoPrint.postJson(base, body);
            guarded(req).done(function () { self.profile(undefined); self.loadProfiles(); });
        };
        self.deleteProfile = function (p) {
            if (!confirm("Delete profile \"" + p.display_name + "\"?")) return;
            var base = api("printers/" + encodeURIComponent(self.current().id()) + "/profiles/" + encodeURIComponent(p.name));
            guarded(OctoPrint.delete(base)).done(self.loadProfiles);
        };

        self.onBeforeBinding = function () {
            self.settings = self.settingsViewModel.settings;
        };

        self.onSettingsShown = function () {
            self.importMessage("");
            self.refreshStatus();
            self.loadPrinters();
        };

        self.importProfile = function () {
            var input = document.getElementById("curaengine5_import_file");
            if (!input.files || !input.files.length) {
                self.importOk(false);
                self.importMessage("Choose a file first.");
                return;
            }
            var file = input.files[0];
            self.importing(true);
            self.importMessage("");
            OctoPrint.upload(api("import"), file, file.name, {
                display_name: self.importDisplayName(),
                "default": self.importDefault() ? "true" : "false",
                allow_overwrite: self.importOverwrite() ? "true" : "false"
            })
                .done(function (r) {
                    self.importOk(true);
                    var msg = "Profile \"" + r.display_name + "\" imported (" + r.settings + " settings, quality " +
                        (r.quality_type || "?") + ", machine " + (r.machine || "?") + ").";
                    if (r.warnings && r.warnings.length) msg += " " + r.warnings.join(" ");
                    self.importMessage(msg);
                    input.value = "";
                })
                .fail(function (xhr) {
                    self.importOk(false);
                    self.importMessage("Import failed: " + (xhr.responseText || xhr.statusText));
                })
                .always(function () {
                    self.importing(false);
                });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: CuraEngine5ViewModel,
        dependencies: ["settingsViewModel"],
        elements: ["#settings_plugin_curaengine5"]
    });
});
