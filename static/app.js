/* WA Bulk Portal – front-end logic (Alpine.js) */
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = '/login'; throw new Error('Not logged in'); }
  let data = null;
  try { data = await res.json(); } catch (_) { /* non-JSON */ }
  if (!res.ok) {
    let msg = data?.detail ?? `Request failed (${res.status})`;
    if (Array.isArray(msg)) msg = msg.map(e => e.msg).join('; ');
    throw new Error(msg);
  }
  return data;
}
const jsonOpts = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function portal() {
  return {
    tabs: [{ id: 'new', label: 'New campaign' }, { id: 'campaigns', label: 'Campaigns & reports' }, { id: 'blocklist', label: 'Blocklist' }, { id: 'settings', label: 'Settings' }],
    tab: 'new',
    toasts: [],
    settings: {}, sForm: {}, apiKeyInput: '',
    conn: { connected: false, loading: false },

    // new campaign
    drag: false, uploading: false, preview: null, phoneColumn: '',
    form: { name: '', message: '' }, imageFile: null, imageUrl: null,
    creating: false, testPhone: '', testing: false,

    // campaigns
    campaigns: [], current: null, recipients: { items: [], total: 0 },
    rFilter: '', rSearch: '', rPage: 1, poller: null,

    // blocklist
    reasonLabels: { stop: 'Replied STOP', abusive: 'Abusive / vulgar reply', not_interested: 'Not interested', other: 'Other' },
    blocked: { items: [], total: 0, all_total: 0 }, bSearch: '', bReason: '', bPage: 1,
    blockForm: { numbers: '', reason: 'stop', note: '' },
    blockDialog: { open: false, phone: '', reason: 'stop', note: '' },

    async init() {
      const h = location.hash.replace('#', '');
      if (this.tabs.some(t => t.id === h)) this.tab = h;
      window.addEventListener('hashchange', () => {
        const id = location.hash.replace('#', '');
        if (this.tabs.some(t => t.id === id) && id !== this.tab) this.go(id);
      });
      await this.loadSettings();
      this.checkConnection();
      this.loadCampaigns();
      this.loadBlocked();
      setInterval(() => this.checkConnection(), 60000);
    },

    go(id) {
      this.tab = id; location.hash = id;
      if (id === 'campaigns') this.loadCampaigns();
      if (id === 'blocklist') this.loadBlocked();
    },

    toast(msg, type = 'info') {
      const id = Date.now() + Math.random();
      this.toasts.push({ id, msg, type });
      setTimeout(() => this.toasts = this.toasts.filter(t => t.id !== id), type === 'error' ? 7000 : 3500);
    },

    // ---------------- settings / connection
    async loadSettings() {
      try {
        this.settings = await api('/api/settings');
        const { min_delay_sec, max_delay_sec, batch_size, batch_pause_sec, daily_limit, max_retries } = this.settings;
        this.sForm = { min_delay_sec, max_delay_sec, batch_size, batch_pause_sec, daily_limit, max_retries };
      } catch (e) { this.toast(e.message, 'error'); }
    },
    async saveSettings(values) {
      if (values.min_delay_sec > values.max_delay_sec) return this.toast('Min delay cannot be more than max delay', 'error');
      try {
        await api('/api/settings', jsonOpts('PUT', values));
        await this.loadSettings();
        this.toast('Settings saved');
        if ('api_key' in values) this.checkConnection(true);
      } catch (e) { this.toast(e.message, 'error'); }
    },
    async checkConnection(verbose = false) {
      this.conn.loading = true;
      try {
        this.conn = { ...(await api('/api/connection')), loading: false };
        if (verbose) this.toast(this.conn.connected ? 'WhatsApp session is connected ✅' : (this.conn.message || `Status: ${this.conn.status}`),
          this.conn.connected ? 'info' : 'error');
      } catch (e) { this.conn = { connected: false, loading: false, message: e.message }; }
    },

    // ---------------- contacts
    async pickContacts(file) {
      if (!file) return;
      this.uploading = true; this.preview = null;
      const fd = new FormData(); fd.append('file', file);
      try {
        this.preview = await api('/api/contacts/preview', { method: 'POST', body: fd });
        this.phoneColumn = this.preview.phone_column;
        if (!this.form.name) this.form.name = file.name.replace(/\.[^.]+$/, '') + ' – ' + new Date().toLocaleDateString();
      } catch (e) { this.toast(e.message, 'error'); }
      this.uploading = false;
    },
    async reparse() {
      const fd = new FormData(); fd.append('phone_column', this.phoneColumn);
      try {
        const filename = this.preview.filename;
        this.preview = { ...(await api(`/api/contacts/preview/${this.preview.upload_id}`, { method: 'POST', body: fd })), filename };
      } catch (e) { this.toast(e.message, 'error'); }
    },
    placeholderCols() {
      const cols = ['name'];
      (this.preview?.columns || []).forEach(c => { if (c !== this.phoneColumn && !cols.includes(c.toLowerCase())) cols.push(c); });
      return cols;
    },
    insertVar(col) {
      const el = this.$refs.msg, token = `{${col}}`;
      const s = el.selectionStart ?? this.form.message.length;
      this.form.message = this.form.message.slice(0, s) + token + this.form.message.slice(el.selectionEnd ?? s);
      this.$nextTick(() => { el.focus(); el.selectionStart = el.selectionEnd = s + token.length; });
    },

    // ---------------- image
    pickImage(file) {
      if (!file) return;
      if (!/^image\/(png|jpe?g)$/.test(file.type)) return this.toast('Only JPG or PNG images', 'error');
      if (file.size > 5 * 1024 * 1024) return this.toast('Image must be under 5 MB', 'error');
      this.clearImage();
      this.imageFile = file; this.imageUrl = URL.createObjectURL(file);
    },
    clearImage() { if (this.imageUrl) URL.revokeObjectURL(this.imageUrl); this.imageFile = null; this.imageUrl = null; },

    // ---------------- preview helpers
    firstVars() { return this.preview?.sample_valid?.[0]?.vars || { name: 'Ali' }; },
    renderPreview() {
      const vars = Object.fromEntries(Object.entries(this.firstVars()).map(([k, v]) => [k.toLowerCase(), v]));
      return this.form.message.replace(/\{\s*([^{}]+?)\s*\}/g, (m, k) => (k.toLowerCase() in vars ? vars[k.toLowerCase()] : m));
    },
    waFormat(text) {
      return escapeHtml(text)
        .replace(/\*([^*\n]+)\*/g, '<b>$1</b>').replace(/_([^_\n]+)_/g, '<i>$1</i>')
        .replace(/~([^~\n]+)~/g, '<s>$1</s>').replace(/```([^`]+)```/g, '<code>$1</code>');
    },
    estimate() {
      const n = this.preview?.valid_count || 0, s = this.settings;
      const avg = ((s.min_delay_sec || 8) + (s.max_delay_sec || 15)) / 2;
      const breaks = s.batch_size ? Math.floor(n / s.batch_size) * (s.batch_pause_sec || 0) : 0;
      const sec = n * avg + breaks;
      return sec < 3600 ? `${Math.ceil(sec / 60)} min` : `${(sec / 3600).toFixed(1)} hours`;
    },
    canSend() {
      return this.preview?.valid_count > 0 && this.form.name.trim() && (this.form.message.trim() || this.imageFile) && this.form.message.length <= 4096;
    },

    async sendTest() {
      this.testing = true;
      try {
        const r = await api('/api/test-message', jsonOpts('POST', { phone: this.testPhone, message: this.renderPreview() || 'Test message ✅' }));
        this.toast(`Test sent to ${r.phone} ✅`);
      } catch (e) { this.toast(e.message, 'error'); }
      this.testing = false;
    },

    async createCampaign() {
      if (!confirm(`Send this message to ${this.preview.valid_count} numbers?`)) return;
      this.creating = true;
      const fd = new FormData();
      fd.append('name', this.form.name.trim());
      fd.append('upload_id', this.preview.upload_id);
      fd.append('phone_column', this.phoneColumn);
      fd.append('filename', this.preview.filename || '');
      fd.append('message', this.form.message);
      fd.append('start_now', 'true');
      if (this.imageFile) fd.append('image', this.imageFile);
      try {
        const c = await api('/api/campaigns', { method: 'POST', body: fd });
        this.toast('Campaign started 🚀');
        this.preview = null; this.form = { name: '', message: '' }; this.clearImage();
        this.go('campaigns'); this.openCampaign(c.id);
      } catch (e) { this.toast(e.message, 'error'); this.loadCampaigns(); }
      this.creating = false;
    },

    // ---------------- campaigns
    async loadCampaigns() {
      try { this.campaigns = await api('/api/campaigns'); } catch (e) { this.toast(e.message, 'error'); }
    },
    async openCampaign(id) {
      this.rFilter = ''; this.rSearch = ''; this.rPage = 1;
      await this.refreshCurrent(id);
      this.loadRecipients();
      clearInterval(this.poller);
      this.poller = setInterval(() => {
        if (!this.current) return clearInterval(this.poller);
        if (this.current.status === 'running' || this.current.is_active) { this.refreshCurrent(this.current.id); this.loadRecipients(); }
      }, 3000);
    },
    closeCampaign() { clearInterval(this.poller); this.current = null; this.loadCampaigns(); },
    async refreshCurrent(id) {
      try { this.current = await api(`/api/campaigns/${id}`); } catch (e) { this.toast(e.message, 'error'); }
    },
    async loadRecipients() {
      if (!this.current) return;
      const p = new URLSearchParams({ page: this.rPage, page_size: 50 });
      if (this.rFilter) p.set('status', this.rFilter);
      if (this.rSearch) p.set('q', this.rSearch);
      try { this.recipients = await api(`/api/campaigns/${this.current.id}/recipients?${p}`); } catch (e) { this.toast(e.message, 'error'); }
    },
    async action(name) {
      try {
        const r = await api(`/api/campaigns/${this.current.id}/${name}`, { method: 'POST' });
        if (name === 'retry-failed') this.toast(`${r.requeued} numbers queued again – press Resume`);
        setTimeout(() => { this.refreshCurrent(this.current.id); this.loadRecipients(); }, 700);
      } catch (e) { this.toast(e.message, 'error'); }
    },
    async deleteCampaign() {
      try { await api(`/api/campaigns/${this.current.id}`, { method: 'DELETE' }); this.toast('Deleted'); this.closeCampaign(); }
      catch (e) { this.toast(e.message, 'error'); }
    },

    // ---------------- blocklist
    async loadBlocked() {
      const p = new URLSearchParams({ page: this.bPage, page_size: 50 });
      if (this.bSearch) p.set('q', this.bSearch);
      if (this.bReason) p.set('reason', this.bReason);
      try { this.blocked = await api(`/api/blocklist?${p}`); } catch (e) { this.toast(e.message, 'error'); }
    },
    blockResultToast(r) {
      let msg = `${r.added} added to blocklist`;
      if (r.already) msg += ` · ${r.already} were already there`;
      if (r.invalid_count) msg += ` · ${r.invalid_count} invalid`;
      this.toast(msg, r.added || r.already ? 'info' : 'error');
    },
    async addBlocked() {
      try {
        const r = await api('/api/blocklist', jsonOpts('POST', this.blockForm));
        this.blockResultToast(r);
        this.blockForm.numbers = ''; this.blockForm.note = '';
        this.loadBlocked();
      } catch (e) { this.toast(e.message, 'error'); }
    },
    async importBlocked(file) {
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file); fd.append('reason', this.blockForm.reason); fd.append('note', this.blockForm.note);
      try { this.blockResultToast(await api('/api/blocklist/import', { method: 'POST', body: fd })); this.loadBlocked(); }
      catch (e) { this.toast(e.message, 'error'); }
    },
    async removeBlocked(id) {
      try { await api(`/api/blocklist/${id}`, { method: 'DELETE' }); this.toast('Removed from blocklist'); this.loadBlocked(); }
      catch (e) { this.toast(e.message, 'error'); }
    },
    openBlockDialog(phone) { this.blockDialog = { open: true, phone, reason: 'stop', note: '' }; },
    async quickBlock() {
      const d = this.blockDialog;
      try {
        this.blockResultToast(await api('/api/blocklist', jsonOpts('POST', { numbers: d.phone, reason: d.reason, note: d.note })));
        d.open = false;
        this.loadBlocked();
      } catch (e) { this.toast(e.message, 'error'); }
    },
    reasonClass(r) {
      return { stop: 'bg-amber-100 text-amber-800', abusive: 'bg-rose-100 text-rose-700',
               not_interested: 'bg-slate-100 text-slate-600', other: 'bg-slate-100 text-slate-600' }[r] || 'bg-slate-100';
    },

    // ---------------- ui helpers
    statusClass(s) {
      return {
        running: 'bg-blue-100 text-blue-700', sending: 'bg-blue-100 text-blue-700', completed: 'bg-emerald-100 text-emerald-700',
        sent: 'bg-emerald-100 text-emerald-700', paused: 'bg-amber-100 text-amber-800', pending: 'bg-slate-100 text-slate-600',
        draft: 'bg-slate-100 text-slate-600', failed: 'bg-rose-100 text-rose-700', cancelled: 'bg-slate-200 text-slate-600',
        skipped: 'bg-slate-100 text-slate-500',
      }[s] || 'bg-slate-100';
    },
    fmtDate(iso) {
      if (!iso) return '';
      const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
      return d.toLocaleString();
    },
  };
}
