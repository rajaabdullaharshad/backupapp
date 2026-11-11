import { createApp } from "https://unpkg.com/vue@3/dist/vue.esm-browser.prod.js";

const emptyProfile = () => ({
  remote: false,
  use_wsl: false,
  access_mode: "root",
  server_ip: "",
  ssh_port: 22,
  ssh_user: "",
  ssh_password: "",
  use_ssh_key: false,
  ssh_private_key: "",
  ssh_key_passphrase: "",
  ssh_public_key: "",
  sudo_password: "",
  bench_version: "version-15",
  use_nvm: true,
  site_admin_password: "admin",
  mariadb_root_password: "root",
});

createApp({
  data() {
    return {
      activeTab: "bench-manager",
      profile: emptyProfile(),
      benches: [],
      selectedSite: {
        bench_path: "",
        site_name: "",
      },
      loadingBenches: false,
      savingProfile: false,
      creatingSite: false,
      migratingSite: false,
      restoringSite: false,
      installingGithubApp: false,
      status: "Ready",
      error: "",
      createForm: {
        bench_path: "",
        site_name: "",
        site_admin_password: "admin",
        mariadb_root_password: "root",
      },
      githubForm: {
        bench_path: "",
        site_name: "",
        repo_url: "",
        branch: "version-15",
        app_name: "",
        github_username: "",
        github_token: "",
        install_on_site: true,
      },
      restoreForm: {
        bench_path: "",
        site_name: "",
        db_backup_path: "",
        public_backup_path: "",
        private_backup_path: "",
      },
    };
  },
  computed: {
    selectedBench() {
      return this.benches.find((bench) => bench.bench_path === this.createForm.bench_path) || null;
    },
    activeBench() {
      return this.benches.find((bench) => bench.bench_path === this.selectedSite.bench_path) || null;
    },
    activeSiteReady() {
      return Boolean(this.selectedSite.bench_path && this.selectedSite.site_name);
    },
    canCreateSite() {
      return Boolean(this.createForm.bench_path && this.createForm.site_name && !this.creatingSite);
    },
    canMigrateSite() {
      return this.activeSiteReady && !this.migratingSite;
    },
    canRestoreSite() {
      return Boolean(this.restoreForm.bench_path && this.restoreForm.site_name && this.restoreForm.db_backup_path && !this.restoringSite);
    },
    canInstallGithubApp() {
      return Boolean(this.githubForm.bench_path && this.githubForm.repo_url && !this.installingGithubApp);
    },
  },
  async mounted() {
    await this.loadProfile();
    await this.loadBenches();
  },
  methods: {
    async api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...options,
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || `Request failed with ${response.status}`);
      }
      return payload;
    },
    async loadProfile() {
      try {
        this.error = "";
        const payload = await this.api("/api/profile");
        this.profile = { ...emptyProfile(), ...payload.profile };
        this.createForm.site_admin_password = this.profile.site_admin_password || "admin";
        this.createForm.mariadb_root_password = this.profile.mariadb_root_password || "root";
      } catch (error) {
        this.error = error.message;
      }
    },
    async saveProfile() {
      try {
        this.savingProfile = true;
        this.error = "";
        this.status = "Saving server profile...";
        const payload = await this.api("/api/profile", {
          method: "POST",
          body: JSON.stringify({ profile: this.profile }),
        });
        this.profile = { ...emptyProfile(), ...payload.profile };
        this.status = "Server profile saved.";
      } catch (error) {
        this.error = error.message;
      } finally {
        this.savingProfile = false;
      }
    },
    async loadBenches() {
      try {
        this.loadingBenches = true;
        this.error = "";
        this.status = "Loading benches and sites...";
        const payload = await this.api("/api/benches/discover", {
          method: "POST",
          body: JSON.stringify({ config: this.profile }),
        });
        this.benches = payload.benches || [];
        if (!this.createForm.bench_path && this.benches.length) {
          this.createForm.bench_path = this.benches[0].bench_path;
        }
        if (!this.githubForm.bench_path && this.benches.length) {
          this.githubForm.bench_path = this.benches[0].bench_path;
        }
        if (!this.restoreForm.bench_path && this.benches.length) {
          this.restoreForm.bench_path = this.benches[0].bench_path;
        }
        if (this.selectedSite.bench_path && this.selectedSite.site_name) {
          const bench = this.benches.find((item) => item.bench_path === this.selectedSite.bench_path);
          if (!bench || !bench.sites.includes(this.selectedSite.site_name)) {
            this.selectedSite = { bench_path: "", site_name: "" };
          }
        }
        this.status = this.benches.length
          ? `Loaded ${this.benches.length} bench(es).`
          : "No benches found on the target.";
      } catch (error) {
        this.error = error.message;
      } finally {
        this.loadingBenches = false;
      }
    },
    async createSite() {
      if (!this.canCreateSite) {
        return;
      }
      try {
        this.creatingSite = true;
        this.error = "";
        this.status = `Creating site ${this.createForm.site_name}...`;
        const payload = await this.api("/api/sites/create", {
          method: "POST",
          body: JSON.stringify({
            config: this.profile,
            ...this.createForm,
          }),
        });
        this.status = payload.stdout || `Site ${this.createForm.site_name} created.`;
        this.selectedSite = {
          bench_path: this.createForm.bench_path,
          site_name: this.createForm.site_name,
        };
        this.githubForm.bench_path = this.createForm.bench_path;
        this.githubForm.site_name = this.createForm.site_name;
        this.restoreForm.bench_path = this.createForm.bench_path;
        this.restoreForm.site_name = this.createForm.site_name;
        await this.loadBenches();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.creatingSite = false;
      }
    },
    selectSite(benchPath, siteName) {
      this.selectedSite = { bench_path: benchPath, site_name: siteName };
      this.githubForm.bench_path = benchPath;
      this.githubForm.site_name = siteName;
      this.restoreForm.bench_path = benchPath;
      this.restoreForm.site_name = siteName;
      this.status = `Selected site ${siteName}.`;
    },
    async migrateSite() {
      if (!this.canMigrateSite) {
        return;
      }
      try {
        this.migratingSite = true;
        this.error = "";
        this.status = `Migrating ${this.selectedSite.site_name}...`;
        const payload = await this.api("/api/sites/migrate", {
          method: "POST",
          body: JSON.stringify({
            config: this.profile,
            bench_path: this.selectedSite.bench_path,
            site_name: this.selectedSite.site_name,
          }),
        });
        this.status = payload.stdout || `Site ${this.selectedSite.site_name} migrated.`;
        await this.loadBenches();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.migratingSite = false;
      }
    },
    async restoreSite() {
      if (!this.canRestoreSite) {
        return;
      }
      try {
        this.restoringSite = true;
        this.error = "";
        this.status = `Restoring ${this.restoreForm.site_name} from backup...`;
        const payload = await this.api("/api/sites/restore", {
          method: "POST",
          body: JSON.stringify({
            config: this.profile,
            ...this.restoreForm,
          }),
        });
        this.status = payload.stdout || `Restore completed for ${this.restoreForm.site_name}.`;
      } catch (error) {
        this.error = error.message;
      } finally {
        this.restoringSite = false;
      }
    },
    async installGithubApp() {
      if (!this.canInstallGithubApp) {
        return;
      }
      try {
        this.installingGithubApp = true;
        this.error = "";
        this.status = `Installing app from GitHub...`;
        const payload = await this.api("/api/apps/install-github", {
          method: "POST",
          body: JSON.stringify({
            config: this.profile,
            ...this.githubForm,
          }),
        });
        this.status = payload.stdout || "GitHub app install completed.";
        await this.loadBenches();
      } catch (error) {
        this.error = error.message;
      } finally {
        this.installingGithubApp = false;
      }
    },
  },
  template: `
    <main class="shell">
      <section class="hero">
        <div>
          <p class="eyebrow">Vue Migration</p>
          <h1>Frappe Auto Deploy Web</h1>
          <p class="lede">Fast bench discovery, bench-aware site creation, and a cleaner path toward GitHub app installs and restore flows.</p>
        </div>
        <div class="status-card">
          <span class="status-label">Status</span>
          <strong>{{ status }}</strong>
          <p v-if="error" class="error">{{ error }}</p>
        </div>
      </section>

      <section class="tabs">
        <button class="tab-button" :class="{ active: activeTab === 'bench-manager' }" @click="activeTab = 'bench-manager'">Bench Manager</button>
        <button class="tab-button" :class="{ active: activeTab === 'server-profile' }" @click="activeTab = 'server-profile'">Server Profile</button>
      </section>

      <section v-if="activeTab === 'bench-manager'" class="tab-panel">
        <section class="grid">
          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>Load Benches</h2>
                <p>Yahan sari benches aur unke sites show honge. Isi tab me bench selection aur site creation rahegi.</p>
              </div>
              <button :disabled="loadingBenches" @click="loadBenches">{{ loadingBenches ? 'Loading...' : 'Load Benches' }}</button>
            </div>
            <div v-if="benches.length" class="bench-list">
              <div v-for="bench in benches" :key="bench.bench_path" class="bench-card">
                <div class="bench-top">
                  <div>
                    <strong>{{ bench.bench }}</strong>
                    <p>{{ bench.bench_path }}</p>
                  </div>
                  <span class="pill">{{ bench.site_count }} sites</span>
                </div>
                <ul>
                  <li v-for="site in bench.sites" :key="site">
                    <button
                      class="site-chip"
                      :class="{ active: selectedSite.bench_path === bench.bench_path && selectedSite.site_name === site }"
                      @click="selectSite(bench.bench_path, site)"
                    >
                      {{ site }}
                    </button>
                  </li>
                </ul>
              </div>
            </div>
            <p v-else class="empty">No benches loaded yet.</p>
          </article>

          <article class="panel create-panel">
            <div class="panel-head">
              <div>
                <h2>Create Site</h2>
                <p>Site create karne se pehle bench choose karo. Aap chahein to har bench ke sites dekh kar decide kar saktay ho.</p>
              </div>
              <button :disabled="!canCreateSite" @click="createSite">{{ creatingSite ? 'Creating...' : 'Create Site' }}</button>
            </div>
            <div class="form-grid">
              <label class="wide">
                <span>Target Bench</span>
                <select v-model="createForm.bench_path">
                  <option disabled value="">Select a bench</option>
                  <option v-for="bench in benches" :key="bench.bench_path" :value="bench.bench_path">
                    {{ bench.bench }} - {{ bench.bench_path }}
                  </option>
                </select>
              </label>
              <label>
                <span>Site Name</span>
                <input v-model="createForm.site_name" placeholder="nextash.com" />
              </label>
              <label>
                <span>Admin Password</span>
                <input v-model="createForm.site_admin_password" type="password" />
              </label>
              <label>
                <span>MariaDB Root Password</span>
                <input v-model="createForm.mariadb_root_password" type="password" />
              </label>
            </div>
            <div v-if="selectedBench" class="selection-note">
              Site <strong>{{ createForm.site_name || '...' }}</strong> will be created inside <strong>{{ selectedBench.bench }}</strong>.
            </div>
          </article>
        </section>

        <section v-if="activeSiteReady" class="grid operations-grid">
          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>Selected Site</h2>
                <p>{{ selectedSite.site_name }} in {{ activeBench ? activeBench.bench : selectedSite.bench_path }}</p>
              </div>
              <button :disabled="!canMigrateSite" @click="migrateSite">{{ migratingSite ? 'Migrating...' : 'Migrate Site' }}</button>
            </div>
            <div class="selection-note">
              Selected site operations yahan show hongi. Isi site par restore, migrate, aur GitHub app install kar sakte ho.
            </div>
          </article>

          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>Install App From GitHub</h2>
                <p>GitHub repo URL do, branch select karo, aur zarurat ho to token bhi do.</p>
              </div>
              <button :disabled="!canInstallGithubApp" @click="installGithubApp">{{ installingGithubApp ? 'Installing...' : 'Install App' }}</button>
            </div>
            <div class="form-grid">
              <label class="wide">
                <span>Target Bench</span>
                <select v-model="githubForm.bench_path">
                  <option disabled value="">Select a bench</option>
                  <option v-for="bench in benches" :key="bench.bench_path" :value="bench.bench_path">
                    {{ bench.bench }} - {{ bench.bench_path }}
                  </option>
                </select>
              </label>
              <label>
                <span>Site</span>
                <input v-model="githubForm.site_name" placeholder="Optional if only get-app chahiye" />
              </label>
              <label>
                <span>App Name</span>
                <input v-model="githubForm.app_name" placeholder="custom_app" />
              </label>
              <label class="wide">
                <span>Repository URL</span>
                <input v-model="githubForm.repo_url" placeholder="https://github.com/org/repo.git" />
              </label>
              <label>
                <span>Branch</span>
                <input v-model="githubForm.branch" placeholder="version-15" />
              </label>
              <label>
                <span>GitHub Username</span>
                <input v-model="githubForm.github_username" placeholder="Optional" />
              </label>
              <label class="wide">
                <span>GitHub Token</span>
                <input v-model="githubForm.github_token" type="password" placeholder="Optional private repo token" />
              </label>
              <label>
                <span>Install On Site</span>
                <input type="checkbox" v-model="githubForm.install_on_site" />
              </label>
            </div>
          </article>
        </section>

        <section v-if="activeSiteReady" class="tab-panel">
          <article class="panel">
            <div class="panel-head">
              <div>
                <h2>Restore Site</h2>
                <p>Backup paths do aur selected site ko restore karo.</p>
              </div>
              <button :disabled="!canRestoreSite" @click="restoreSite">{{ restoringSite ? 'Restoring...' : 'Restore Site' }}</button>
            </div>
            <div class="form-grid">
              <label class="wide">
                <span>Target Bench</span>
                <select v-model="restoreForm.bench_path">
                  <option disabled value="">Select a bench</option>
                  <option v-for="bench in benches" :key="bench.bench_path" :value="bench.bench_path">
                    {{ bench.bench }} - {{ bench.bench_path }}
                  </option>
                </select>
              </label>
              <label>
                <span>Site Name</span>
                <input v-model="restoreForm.site_name" placeholder="nextash.com" />
              </label>
              <label class="wide">
                <span>Database Backup Path</span>
                <input v-model="restoreForm.db_backup_path" placeholder="/home/nextash/frappe-backups/site/database.sql.gz" />
              </label>
              <label class="wide">
                <span>Public Files Backup Path</span>
                <input v-model="restoreForm.public_backup_path" placeholder="/home/nextash/frappe-backups/site/files.tar" />
              </label>
              <label class="wide">
                <span>Private Files Backup Path</span>
                <input v-model="restoreForm.private_backup_path" placeholder="/home/nextash/frappe-backups/site/private-files.tar" />
              </label>
            </div>
          </article>
        </section>
      </section>

      <section v-if="activeTab === 'server-profile'" class="tab-panel">
        <article class="panel">
          <div class="panel-head">
            <h2>Server Profile</h2>
            <button class="ghost" :disabled="savingProfile" @click="saveProfile">{{ savingProfile ? 'Saving...' : 'Save' }}</button>
          </div>
          <div class="form-grid">
            <label>
              <span>Remote Server</span>
              <input type="checkbox" v-model="profile.remote" />
            </label>
            <label>
              <span>Access Mode</span>
              <select v-model="profile.access_mode">
                <option value="root">Root</option>
                <option value="user">User</option>
              </select>
            </label>
            <label>
              <span>Server IP</span>
              <input v-model="profile.server_ip" placeholder="206.189.x.x" />
            </label>
            <label>
              <span>SSH Port</span>
              <input v-model.number="profile.ssh_port" type="number" min="1" />
            </label>
            <label>
              <span>SSH User</span>
              <input v-model="profile.ssh_user" placeholder="root or nextash" />
            </label>
            <label>
              <span>SSH Password</span>
              <input v-model="profile.ssh_password" type="password" />
            </label>
            <label>
              <span>Use SSH Key Login</span>
              <input type="checkbox" v-model="profile.use_ssh_key" />
            </label>
            <label class="wide">
              <span>SSH Private Key</span>
              <textarea v-model="profile.ssh_private_key" rows="5" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>
            </label>
            <label>
              <span>Key Passphrase</span>
              <input v-model="profile.ssh_key_passphrase" type="password" placeholder="Only if needed" />
            </label>
            <label class="wide">
              <span>SSH Public Key</span>
              <textarea v-model="profile.ssh_public_key" rows="3" placeholder="Optional: install this public key on server"></textarea>
            </label>
            <label>
              <span>Sudo Password</span>
              <input v-model="profile.sudo_password" type="password" />
            </label>
            <label>
              <span>Bench Version</span>
              <select v-model="profile.bench_version">
                <option value="version-13">version-13</option>
                <option value="version-14">version-14</option>
                <option value="version-15">version-15</option>
                <option value="version-16">version-16</option>
              </select>
            </label>
          </div>
        </article>
      </section>
    </main>
  `,
}).mount("#app");
