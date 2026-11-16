/*
 * OctoPrint-CuraEngine5 — "Slicer" tab: 3D view, model transformation, slicing.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
$(function () {
    // Binds a Bootstrap modal to an observable: shown when truthy, observable cleared on close
    ko.bindingHandlers.curaengine5Modal = {
        init: function (element, accessor) {
            var $el = $(element);
            // moved out of the tab (after bindings are applied): the modal does not depend on the tab being displayed
            setTimeout(function () { $el.appendTo(document.body); }, 0);
            ko.utils.domNodeDisposal.addDisposeCallback(element, function () { $el.remove(); });
            $el.on("hidden", function (e) {
                if (e.target !== element) return;
                var obs = accessor();
                if (ko.isObservable(obs) && obs()) obs(typeof obs() === "boolean" ? false : undefined);
            });
        },
        update: function (element, accessor) {
            var open = !!ko.unwrap(accessor()), $el = $(element);
            if (open && !$el.is(":visible")) $el.modal({backdrop: "static", keyboard: false, show: true});
            else if (!open && $el.is(":visible")) $el.modal("hide");
        }
    };

    function CuraEngine5TabViewModel(parameters) {
        var self = this;
        var slicingVM = parameters[0];
        var settingsVM = parameters[1];
        var printerStateVM = parameters[2];
        var assets = function (path) { return BASEURL + "plugin/curaengine5/static/" + path; };
        var api = function (path) { return OctoPrint.getBlueprintUrl("curaengine5") + path; };

        // labels of the profile selector
        self.txt = {
            profile: "Print profile", machineDefault: "Machine default profile",
            originGroup: "Original profiles (read-only)", mineGroup: "My profiles"
        };
        self.printers = ko.observableArray([]);
        self.printerId = ko.observable(undefined);
        self.profiles = ko.observableArray([]);
        self.profileName = ko.observable("");
        self.models = ko.observableArray([]);
        self.modelPath = ko.observable(undefined);
        self.mode = ko.observable("translate");
        self.picking = ko.observable(false);
        self.error = ko.observable("");
        self.hasModel = ko.observable(false);
        self.fits = ko.observable(true);
        self.sizeText = ko.observable("");
        self.slicing = ko.observable(false);
        self.progress = ko.observable(0);
        self.phase = ko.observable("idle");   // idle | slicing | done | error
        self.stage = ko.observable("");
        self.shortError = ko.observable("");
        self.cancelling = ko.observable(false);
        self.supports = ko.observable("");
        self.view = ko.observable("model");
        self.previewReady = ko.observable(false);
        self.analysing = ko.observable(false);
        self.layer = ko.observable(1);
        self.layerMax = ko.observable(0);
        self.layerTypes = ko.observableArray([]);
        self.buttonText = ko.pureComputed(function () {
            switch (self.phase()) {
                case "slicing": return "Slicing… " + self.progress() + "%";
                case "done": return "Done";
                case "error": return self.shortError() || "Failed";
                default: return "Slice";
            }
        });
        // the G-code always takes the STL's name (same folder)
        self.gcodeName = ko.pureComputed(function () {
            return self.modelPath() ? self.modelPath().replace(/\.stl$/i, "") + ".gcode" : "";
        });
        self.overwriteAsk = ko.observable("");   // name of the file to overwrite, confirmation dialog open when non-empty
        self.uniform = ko.observable(true);
        var names = ["x", "y", "rx", "ry", "rz", "sx", "sy", "sz"];
        _.each(names, function (n) { self[n] = ko.observable("0"); });
        // slicing would compete with the print for the Pi's CPU and can stall it
        self.printBusy = ko.pureComputed(function () {
            return !!(printerStateVM && (printerStateVM.isPrinting() || printerStateVM.isPaused()));
        });
        self.canSlice = ko.pureComputed(function () {
            return self.hasModel() && !!self.printerId() && self.phase() !== "slicing" && !self.printBusy();
        });

        var scene = null, loading = null, volume = null, silent = false;

        var ensureScene = function () {
            if (loading) return loading;
            // import map: three.js is bundled in the plugin (no CDN)
            var map = document.createElement("script");
            map.type = "importmap";
            map.textContent = JSON.stringify({imports: {three: assets("vendor/three/three.module.js")}});
            document.head.appendChild(map);
            loading = import(assets("js/curaengine5_scene.js")).then(function (m) {
                scene = m.createScene(document.getElementById("curaengine5_canvas"));
                scene.listeners.push(fromScene);
                if (volume) scene.setVolume(volume.w, volume.d, volume.h, volume.c);
                return scene;
            }).catch(function (e) {
                self.error("Vue 3D indisponible : " + e);
            });
            return loading;
        };

        var fromScene = function (s) {
            silent = true;
            _.each(names, function (n) { self[n](String(Math.round(s.state[n] * 100) / 100)); });
            silent = false;
            self.fits(s.fits);
            self.sizeText(s.size ? "Dimensions : " + _.map(s.size, function (v) { return v.toFixed(1); }).join(" × ") + " mm" : "");
        };
        var toScene = function () {
            if (silent || !scene) return;
            var patch = {};
            _.each(names, function (n) { var v = parseFloat(String(self[n]()).replace(",", ".")); if (!isNaN(v)) patch[n] = v; });
            scene.setState(patch);
        };
        _.each(names, function (n) {
            self[n].subscribe(function (v) {
                if (silent) return;
                var f = parseFloat(String(v).replace(",", "."));
                if (self.uniform() && /^s[xyz]$/.test(n) && !isNaN(f)) {
                    silent = true;
                    _.each(["sx", "sy", "sz"], function (o) { if (o !== n) self[o](String(f)); });
                    silent = false;
                }
                toScene();
            });
        });

        // --- printers, profiles, models
        self.loadPrinters = function () {
            OctoPrint.get(api("printers")).done(function (r) {
                self.printers(r.printers || []);
                if (!self.printerId() && r.printers && r.printers.length) {
                    var def = _.find(r.printers, function (p) { return p["default"]; }) || r.printers[0];
                    self.printerId(def.id);
                }
            });
        };
        self.printerId.subscribe(function (id) {
            self.profiles([]);
            self.profileName("");
            if (!id) return;
            OctoPrint.get(api("printers/" + encodeURIComponent(id))).done(function (p) {
                var g = p.settings.global, val = function (k) { return g[k].value; };
                volume = {w: val("machine_width"), d: val("machine_depth"), h: val("machine_height"), c: !!val("machine_center_is_zero")};
                ensureScene().then(function () { scene.setVolume(volume.w, volume.d, volume.h, volume.c); });
            });
            OctoPrint.get(api("printers/" + encodeURIComponent(id) + "/profiles?shared=1")).done(function (r) {
                self.profiles(r.profiles || []);
                var cur = _.find(self.printers(), function (p) { return p.id === id; });
                var wanted = cur && cur.default_profile;
                if (wanted && _.some(r.profiles, function (p) { return p.name === wanted; })) self.profileName(wanted);
            });
        });
        var collect = function (nodes, out) {
            _.each(nodes || [], function (n) {
                if (n.type === "folder") collect(n.children, out);
                else if (n.type === "model" && /\.stl$/i.test(n.name)) out.push(n);
            });
            return out;
        };
        self.loadModels = function (select) {
            OctoPrint.files.list("local", true).done(function (r) {
                self.models(collect(r.files, []));
                if (select) self.modelPath(select);
            });
        };
        self.modelPath.subscribe(function (path) {
            if (!path) return;
            var node = _.find(self.models(), function (m) { return m.path === path; });
            if (!node) return;
            var xhr = new XMLHttpRequest();
            xhr.open("GET", node.refs.download);
            xhr.responseType = "arraybuffer";
            xhr.onload = function () {
                ensureScene().then(function () {
                    try {
                        scene.loadBuffer(xhr.response);
                        self.clearPreview();
                        self.hasModel(true);
                        self.error("");
                    } catch (e) {
                        self.error("STL illisible : " + e);
                    }
                });
            };
            xhr.onerror = function () { self.error("Could not download the model."); };
            xhr.send();
        });
        self.uploadName = ko.observable("");
        self.pickFile = function () { document.getElementById("curaengine5_tab_upload").click(); };
        self.fileChosen = function (data, event) {
            var f = event.target.files && event.target.files[0];
            self.uploadName(f ? f.name : "");
        };
        self.uploadModel = function () {
            var input = document.getElementById("curaengine5_tab_upload");
            if (!input.files || !input.files.length) { self.error("Choose an STL file first."); return; }
            var file = input.files[0];
            OctoPrint.files.upload("local", file).done(function (r) {
                input.value = "";
                self.uploadName("");
                self.loadModels(r.files && r.files.local ? r.files.local.path : file.name);
            }).fail(function (xhr) { self.error("Envoi impossible : " + (xhr.responseText || xhr.statusText)); });
        };

        // --- model actions
        var withScene = function (fn) { return function () { if (scene && scene.hasMesh()) fn(); }; };
        self.setMode = function (m) { self.mode(m); if (scene) scene.setMode(m); };
        self.layFlat = withScene(function () { scene.layFlatLargest(); });
        self.togglePick = withScene(function () { self.picking(!self.picking()); scene.setPickMode(self.picking()); });
        self.centerOnBed = withScene(function () { scene.centerOnBed(); });
        self.resetTransform = withScene(function () { scene.reset(); });

        // --- layer preview (G-code parsed in the browser)
        self.clearPreview = function () {
            if (scene) { scene.clearGcode(); scene.setPreview(false); }
            self.previewReady(false);
            self.layerTypes([]);
            self.layerMax(0);
            self.view("model");
        };
        self.setView = function (v) {
            self.view(v);
            if (scene) scene.setPreview(v === "preview");
        };
        self.layer.subscribe(function (n) { if (scene && self.view() === "preview") scene.setLayer(parseInt(n, 10) || 1); });
        self.loadPreview = function (path) {
            self.analysing(true);
            $.ajax({url: BASEURL + "downloads/files/local/" + path.split("/").map(encodeURIComponent).join("/"), dataType: "text"})
                .done(function (text) {
                    // let the UI repaint before parsing (large file)
                    setTimeout(function () {
                        try {
                            var count = scene.loadGcode(text);
                            if (!count) { self.analysing(false); return; }
                            self.layerTypes(_.map(scene.layerTypes, function (t) {
                                var vis = ko.observable(t.type !== "TRAVEL");
                                vis.subscribe(function (on) { scene.setTypeVisible(t.type, on); });
                                return {type: t.type, label: t.label, color: t.color, visible: vis};
                            }));
                            self.layerMax(count);
                            self.layer(count);
                            self.previewReady(true);
                            self.setView("preview");
                        } catch (e) {
                            self.error("Preview failed: " + e);
                        }
                        self.analysing(false);
                    }, 50);
                })
                .fail(function () { self.analysing(false); self.error("G-code unreadable for the preview."); });
        };

        // --- printer settings (like Cura's "Machine Settings")
        self.printerEdit = ko.observable(undefined);
        self.printerOpen = ko.observable(false);
        self.printerEdit.subscribe(function (m) { self.printerOpen(!!m); });
        self.printerOpen.subscribe(function (open) { if (!open && self.printerEdit()) self.printerEdit(undefined); });
        self.printerMessage = ko.observable("");
        self.editPrinter = function () {
            if (!self.printerId()) return;
            self.printerMessage("");
            OctoPrint.get(api("printers/" + encodeURIComponent(self.printerId()))).done(function (p) {
                self.printerEdit(new window.CuraEngine5PrinterModel(p));
            }).fail(function (xhr) { self.error(xhr.responseText || xhr.statusText); });
        };
        self.closePrinter = function () {
            var m = self.printerEdit();
            if (m && m.isDirty() && !window.confirm("Close without saving the printer changes?")) return;
            self.printerEdit(undefined);
        };
        var applyVolume = function (p) {
            var g = p.settings.global;
            volume = {w: g.machine_width.value, d: g.machine_depth.value, h: g.machine_height.value, c: !!g.machine_center_is_zero.value};
            if (scene) scene.setVolume(volume.w, volume.d, volume.h, volume.c);   // the view follows the new volume
        };
        self.savePrinter = function () {
            var id = self.printerId();
            self.busy(true);
            OctoPrint.putJson(api("printers/" + encodeURIComponent(id)), self.printerEdit().body()).done(function (p) {
                applyVolume(p);
                self.printerMessage("Saved: the OctoPrint printer profile is up to date.");
                self.error("");
                OctoPrint.get(api("printers")).done(function (r) {
                    self.printers(r.printers || []);
                    self.printerId(id);
                    self.printerEdit(new window.CuraEngine5PrinterModel(p));
                    self.printerMessage("Saved: the OctoPrint printer profile is up to date.");
                });
            }).fail(function (xhr) { self.error(xhr.responseText || xhr.statusText); })
              .always(function () { self.busy(false); });
        };
        // add wizard: the new printer is selected and its settings open
        self.wizard = new window.CuraEngine5Wizard(api, function (p) {
            OctoPrint.get(api("printers")).done(function (r) {
                self.printers(r.printers || []);
                self.printerId(p.id);
                self.printerMessage("");
                // let the wizard close before opening the settings dialog
                setTimeout(function () { self.printerEdit(new window.CuraEngine5PrinterModel(p)); }, 450);
            });
        }, function (msg) { self.error(msg); });
        self.printerId.subscribe(function () { self.printerEdit(undefined); });

        // --- profile editing (reuses the settings page model)
        self.profile = ko.observable(undefined);
        self.profileOpen = ko.observable(false);
        // changes kept for slicing while the dialog is closed
        self.pendingProfile = ko.pureComputed(function () { return !!self.profile() && !self.profileOpen() && self.profile().dirty(); });
        self.materials = ko.observableArray([]);
        self.qualityTypes = ko.observableArray([]);
        self.profileWarnings = ko.observableArray([]);
        self.busy = ko.observable(false);
        var uiUrl = function (what) { return api("printers/" + encodeURIComponent(self.printerId()) + "/ui/" + what); };
        var loadOptions = function (material, quality, done) {
            var qs = "?material=" + encodeURIComponent(material || "") + "&quality_type=" + encodeURIComponent(quality || "");
            OctoPrint.get(api("printers/" + encodeURIComponent(self.printerId()) + "/options" + qs)).done(function (o) {
                self.materials(o.materials || []);
                self.qualityTypes(o.quality_types || []);
                self.profileWarnings(o.warnings || []);
                if (done) done(o);
            });
        };
        var openProfile = function (name, displayName, material, quality, overrides) {
            loadOptions(material, quality, function (o) {
                var prof = new window.CuraEngine5Profile({
                    uiUrl: uiUrl, onChoice: function (p) { loadOptions(p.material(), p.quality_type()); }
                }, name, displayName, material || o.material, quality || o.quality_type, overrides);
                self.profile(prof);
                self.profileOpen(true);
                prof.loadRows();
            });
        };
        self.originProfiles = ko.pureComputed(function () { return _.filter(self.profiles(), function (p) { return !p.printer; }); });
        self.ownProfiles = ko.pureComputed(function () { return _.filter(self.profiles(), function (p) { return !!p.printer; }); });
        // --- printer and default profile
        self.currentPrinter = ko.pureComputed(function () {
            return _.find(self.printers(), function (p) { return p.id === self.printerId(); });
        });
        self.canMakePrinterDefault = ko.pureComputed(function () {
            var p = self.currentPrinter();
            return !!p && !p["default"];
        });
        self.makePrinterDefault = function () {
            var id = self.printerId();
            OctoPrint.putJson(api("printers/" + encodeURIComponent(id)), {"default": true}).done(function () {
                OctoPrint.get(api("printers")).done(function (r) { self.printers(r.printers || []); self.printerId(id); });
            }).fail(function (xhr) { self.error(xhr.responseText || xhr.statusText); });
        };
        // the "default profile" is per printer; empty = machine default profile
        self.canMakeProfileDefault = ko.pureComputed(function () {
            var p = self.currentPrinter();
            return !!p && self.profileName() !== (p.default_profile || "");
        });
        self.makeProfileDefault = function () {
            var id = self.printerId(), name = self.profileName() || "";
            OctoPrint.putJson(api("printers/" + encodeURIComponent(id)), {default_profile: name}).done(function () {
                OctoPrint.get(api("printers")).done(function (r) { self.printers(r.printers || []); self.printerId(id); self.profileName(name); });
            }).fail(function (xhr) { self.error(xhr.responseText || xhr.statusText); });
        };
        self.selectedProfile = ko.pureComputed(function () {
            return _.find(self.profiles(), function (x) { return x.name === self.profileName(); });
        });
        // only profiles created for the printer are editable (not the default, nor imported profiles)
        self.canEditProfile = ko.pureComputed(function () {
            var p = self.selectedProfile();
            return !!(p && p.printer);
        });
        self.duplicateSelected = function () {
            var p = self.selectedProfile();
            if (p) openProfile(null, p.display_name + " (copie)", p.material, p.quality_type, p.user);
        };
        self.newProfile = function () { openProfile(null, "New profile", "", "", {}); };
        self.editSelected = function () {
            var p = _.find(self.profiles(), function (x) { return x.name === self.profileName(); });
            if (!p) return;
            if (!p.printer) { self.error("This imported profile cannot be edited here: create a \"New profile\"."); return; }
            openProfile(p.name, p.display_name, p.material, p.quality_type, p.user);
        };
        self.cancelProfile = function () {
            var p = self.profile();
            if (p && p.dirty() && !window.confirm("Close and discard unsaved changes?")) return;
            self.profileOpen(false);
            self.profile(undefined);
        };
        // closes the dialog, keeping the values for the next slice
        self.keepProfile = function () { self.profileOpen(false); };
        self.reopenProfile = function () { if (self.profile()) self.profileOpen(true); };
        self.discardProfile = function () { self.profile(undefined); };
        var saved = function (prof, name) {
            prof.name = name;
            prof.dirty(false);
            OctoPrint.get(api("printers/" + encodeURIComponent(self.printerId()) + "/profiles?shared=1")).done(function (r) {
                self.profiles(r.profiles || []);
                self.profileName(name);
            });
        };
        var persist = function (asNew) {
            var prof = self.profile();
            var name = prof.display_name();
            if (asNew) {
                name = window.prompt("New profile name", name === "New profile" ? "" : name);
                if (!name) return;
            }
            var body = {display_name: name, material: prof.material(), quality_type: prof.quality_type(), user: prof.overrides};
            var base = api("printers/" + encodeURIComponent(self.printerId()) + "/profiles");
            var req = (prof.name && !asNew) ? OctoPrint.putJson(base + "/" + encodeURIComponent(prof.name), body)
                                           : OctoPrint.postJson(base, body);
            self.busy(true);
            req.done(function (r) { prof.display_name(name); saved(prof, r.name); self.error(""); self.profileOpen(false); self.profile(undefined); })
               .fail(function (xhr) { self.error(xhr.responseText || xhr.statusText); })
               .always(function () { self.busy(false); });
        };
        self.saveProfile = function () { persist(false); };
        self.saveProfileAs = function () { persist(true); };
        self.printerId.subscribe(function () { self.profileOpen(false); self.profile(undefined); });

        // --- slicing
        var startSlice = function (overwrite) {
            var s = scene.state;
            self.progress(0);
            self.stage("Preparing");
            self.cancelling(false);
            self.phase("slicing");
            self.slicing(true);
            self.error("");
            OctoPrint.postJson(api("slice"), {
                file: self.modelPath(), printer: self.printerId(), profile: self.profileName() || null,
                edited: self.profile() ? {material: self.profile().material(), quality_type: self.profile().quality_type(),
                                          user: self.profile().overrides} : undefined,
                supports: self.supports(), overwrite: !!overwrite,
                transform: {position: {x: s.x, y: s.y}, rotation: {x: s.rx, y: s.ry, z: s.rz},
                            scale: {x: s.sx, y: s.sy, z: s.sz}}
            }).fail(function (xhr) {
                self.slicing(false);
                var r = xhr.responseJSON || {};
                if (xhr.status === 409 && r.exists) {            // the G-code exists: ask for confirmation
                    self.phase("idle");
                    self.overwriteAsk(r.gcode);
                    setTimeout(function () { $(".curaengine5-no:visible").focus(); }, 400);   // "No" by default
                    return;
                }
                self.finish("error", r.error || xhr.responseText || xhr.statusText);
            });
        };
        self.slice = function () { startSlice(false); };
        self.overwriteNo = function () { self.overwriteAsk(""); };
        self.overwriteYes = function () { self.overwriteAsk(""); startSlice(true); };
        self.cancelSlice = function () {
            self.cancelling(true);
            OctoPrint.postJson(api("slice/cancel"), {});
        };
        var resetTimer = null;
        self.finish = function (phase, message) {
            self.slicing(false);
            self.shortError(phase === "error" ? String(message || "").substr(0, 60) : "");
            if (phase === "error") self.error(message || "");
            self.phase(phase);
            clearTimeout(resetTimer);
            resetTimer = setTimeout(function () { self.phase("idle"); self.progress(0); }, phase === "done" ? 2000 : 6000);
        };
        self.onDataUpdaterPluginMessage = function (plugin, data) {
            if (plugin !== "curaengine5" || !data) return;
            if (data.type === "slice_progress") {
                if (data.progress >= self.progress()) self.progress(data.progress);   // never go backwards
                self.stage(data.stage || "");
                return;
            }
            if (data.type !== "slice_done") return;
            if (data.ok) { self.progress(100); if (scene) self.loadPreview(data.gcode); }
            self.finish(data.ok ? "done" : "error", data.error);
            var path = data.gcode;
            var load = function (print) {
                OctoPrint.files.select("local", path, print).fail(function (xhr) {
                    new PNotify({title: "Could not load", text: xhr.responseText || xhr.statusText, text_escape: true, type: "error"});
                });
            };
            new PNotify({
                title: data.ok ? "Slicing complete" : "Slicing failed",
                // path and error come from the server (file names, engine output): render them as plain text
                text: data.ok ? path + " is in the OctoPrint files." : data.error,
                text_escape: true, title_escape: true,
                type: data.ok ? "success" : "error", hide: !data.ok ? false : false,
                confirm: data.ok ? {
                    confirm: true,
                    buttons: [
                        {text: "Load", addClass: "btn-mini", click: function (n) { n.remove(); load(false); }},
                        {text: "Print", addClass: "btn-mini", click: function (n) { n.remove(); load(true); }},
                        {text: "Close", addClass: "btn-mini", click: function (n) { n.remove(); }}
                    ]
                } : {confirm: false},
                buttons: {closer: true, sticker: false}
            });
        };

        // Simplified flow: when CuraEngine 5 is the only configured slicer, uploading an STL
        // no longer opens OctoPrint's slicing dialog; the "Slice" button in the file list
        // opens the Slicer tab with the model.
        var onlyOurs = function () {
            var configured = slicingVM.configuredSlicers();
            return configured.length === 1 && configured[0].key === "curaengine5";
        };
        self.openModelInTab = function (path) {
            $('#tabs a[href="#tab_plugin_curaengine5"]').tab("show");
            self.loadModels(path);
        };
        self.onStartupComplete = function () {
            var original = slicingVM.show;
            slicingVM.show = function (target, file, force) {
                if (!onlyOurs()) return original.apply(slicingVM, arguments);
                var setting = (settingsVM.settings.plugins.curaengine5.after_upload || function () { return "tab"; })();
                if (target === "local" && /\.stl$/i.test(file || "") && (force || setting !== "none")) self.openModelInTab(file);
                // without "force": automatic dialog after upload, suppressed
            };
        };

        self.onAfterTabChange = function (current) {
            if (current !== "#tab_plugin_curaengine5") return;
            self.loadPrinters();
            self.loadModels();
            ensureScene().then(function () { scene.resize(); });
        };
    }

    OCTOPRINT_VIEWMODELS.push({
        construct: CuraEngine5TabViewModel,
        dependencies: ["slicingViewModel", "settingsViewModel", "printerStateViewModel"],
        elements: ["#tab_plugin_curaengine5"]
    });
});
