/*
 * Send to see_also - Obsidian plugin.
 * Sends the selection, or else the whole note (without frontmatter), to the
 * locally running see_also (POST /briefkasten) and opens see_also in the browser.
 * Read-only: nothing is written to the vault except the plugin's own settings
 * (data.json in the plugin folder). No network traffic except to the configured
 * see_also address (default: http://127.0.0.1:5001).
 */
'use strict';

const { Plugin, Notice, MarkdownView, PluginSettingTab, Setting, requestUrl } = require('obsidian');

const DEFAULT_SETTINGS = {
    vaiUrl: 'http://127.0.0.1:5001',
    openBrowser: true,
};

function stripFrontmatter(text) {
    // YAML frontmatter only appears at the very top, between two '---' lines.
    const m = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(\r?\n|$)/.exec(text);
    return m ? text.slice(m[0].length) : text;
}

function baseUrl(url) {
    return String(url || '').trim().replace(/\/+$/, '');
}

class SendToSeeAlsoPlugin extends Plugin {
    async onload() {
        this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());

        this.addRibbonIcon('search', 'Send to see_also', () => this.sendRequest());

        this.addCommand({
            id: 'send',
            name: 'Send selection or note',
            callback: () => this.sendRequest(),
        });

        this.registerEvent(
            this.app.workspace.on('editor-menu', (menu) => {
                menu.addItem((item) => item
                    .setTitle('Send to see_also')
                    .setIcon('search')
                    .onClick(() => this.sendRequest()));
            })
        );

        this.addSettingTab(new SendToSeeAlsoSettingTab(this.app, this));
    }

    collectText() {
        const view = this.app.workspace.getActiveViewOfType(MarkdownView);
        // Selection in the editor, otherwise selection in reading view.
        let selection = '';
        if (view && view.editor && view.getMode && view.getMode() === 'source') {
            selection = view.editor.getSelection() || '';
        }
        if (!selection.trim()) {
            const winSel = window.getSelection ? window.getSelection() : null;
            selection = winSel ? winSel.toString() : '';
        }
        const file = view && view.file ? view.file : this.app.workspace.getActiveFile();
        const quelle = file ? file.path : '';
        if (selection.trim()) {
            return { text: selection, art: 'selection', quelle };
        }
        if (view && view.editor) {
            return { text: stripFrontmatter(view.editor.getValue()), art: 'whole note', quelle };
        }
        return { text: '', art: '', quelle };
    }

    async sendRequest() {
        const { text, art, quelle } = this.collectText();
        if (!text.trim()) {
            new Notice('see_also: no text. Open a note or select some text.');
            return;
        }
        const url = baseUrl(this.settings.vaiUrl);
        let response;
        try {
            response = await requestUrl({
                url: url + '/briefkasten',
                method: 'POST',
                contentType: 'application/json',
                body: JSON.stringify({ text, art, quelle, herkunft: 'Obsidian' }),
                throw: false,
            });
        } catch (e) {
            new Notice('see_also is not running (' + url + ' cannot be reached). Start see_also and send again.', 8000);
            return;
        }
        let data = null;
        try { data = response.json; } catch (e) { data = null; }
        if (response.status !== 200 || !data || !data.ok) {
            const reason = data && data.error ? data.error : 'HTTP ' + response.status;
            new Notice('see_also refused the request: ' + reason, 8000);
            return;
        }
        const b = data.brief || {};
        let msg = 'Sent to see_also (' + art + ', ' + b.zeichen + ' characters)';
        if (b.gekuerzt) {
            msg += '. Shortened: only the first ' + b.zeichen_gesucht + ' characters were searched';
        }
        if (b.treffer !== undefined) {
            msg += '. ' + b.treffer + ' documents';
            if (b.bester_score !== null && b.bester_score !== undefined) {
                msg += ', best score ' + b.bester_score;
            }
        }
        new Notice(msg + '.', b.gekuerzt ? 8000 : 4000);
        if (this.settings.openBrowser) {
            window.open(url + '/');
        }
    }
}

class SendToSeeAlsoSettingTab extends PluginSettingTab {
    constructor(app, plugin) {
        super(app, plugin);
        this.plugin = plugin;
    }

    display() {
        const { containerEl } = this;
        containerEl.empty();

        new Setting(containerEl)
            .setName('see_also address')
            .setDesc('Default: http://127.0.0.1:5001')
            .addText((text) => text
                .setPlaceholder(DEFAULT_SETTINGS.vaiUrl)
                .setValue(this.plugin.settings.vaiUrl)
                .onChange(async (value) => {
                    this.plugin.settings.vaiUrl = value.trim() || DEFAULT_SETTINGS.vaiUrl;
                    await this.plugin.saveData(this.plugin.settings);
                }));

        new Setting(containerEl)
            .setName('Open see_also in the browser')
            .setDesc('When off, an already open see_also page shows the request by itself and no new tab is opened.')
            .addToggle((toggle) => toggle
                .setValue(this.plugin.settings.openBrowser)
                .onChange(async (value) => {
                    this.plugin.settings.openBrowser = value;
                    await this.plugin.saveData(this.plugin.settings);
                }));
    }
}

module.exports = SendToSeeAlsoPlugin;
