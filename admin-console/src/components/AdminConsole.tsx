import React, { useState, useEffect, useCallback } from 'react';
import {
  listSkills,
  createSkill,
  updateSkill,
  deleteSkill,
  listUsers,
  getSettings,
  updateSettings,
  listSessions,
  stopSession,
  listSkillFiles,
  getUploadUrl,
  uploadSkillFile,
  getDownloadUrl,
  deleteSkillFile,
  SkillItem,
  SkillInput,
  SkillFile,
  SessionInfo,
} from '../api/adminApi';

// Skill name validation (matches Strands SDK pattern)
const SKILL_NAME_RE = /^(?!-)(?!.*--)(?!.*-$)[a-z0-9-]{1,64}$/;

// Available Bedrock model IDs for the model selector
const AVAILABLE_MODELS = [
  { id: 'moonshotai.kimi-k2.5', label: 'Kimi K2.5 (Moonshot)' },
  { id: 'moonshot.kimi-k2-thinking', label: 'Kimi K2 Thinking (Moonshot)' },
  { id: '', label: '── Claude 4.6 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6' },
  { id: 'us.anthropic.claude-opus-4-6-v1', label: 'Claude Opus 4.6' },
  { id: '', label: '── Claude 4.5 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0', label: 'Claude Sonnet 4.5' },
  { id: 'us.anthropic.claude-opus-4-5-20251101-v1:0', label: 'Claude Opus 4.5' },
  { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5' },
  { id: '', label: '── Claude 4 ──', disabled: true },
  { id: 'us.anthropic.claude-sonnet-4-20250514-v1:0', label: 'Claude Sonnet 4' },
  { id: 'us.anthropic.claude-opus-4-20250514-v1:0', label: 'Claude Opus 4' },
  { id: 'us.anthropic.claude-opus-4-1-20250805-v1:0', label: 'Claude Opus 4.1' },
  { id: '', label: '── Claude 3.x ──', disabled: true },
  { id: 'us.anthropic.claude-3-7-sonnet-20250219-v1:0', label: 'Claude 3.7 Sonnet' },
  { id: 'us.anthropic.claude-3-5-haiku-20241022-v1:0', label: 'Claude 3.5 Haiku' },
  { id: '', label: '── DeepSeek ──', disabled: true },
  { id: 'deepseek.v3.2', label: 'DeepSeek V3.2' },
  { id: 'deepseek.v3-v1:0', label: 'DeepSeek V3.1' },
  { id: 'deepseek.r1-v1:0', label: 'DeepSeek R1' },
  { id: '', label: '── Qwen ──', disabled: true },
  { id: 'qwen.qwen3-235b-a22b-2507-v1:0', label: 'Qwen3 235B A22B' },
  { id: 'qwen.qwen3-next-80b-a3b', label: 'Qwen3 Next 80B A3B' },
  { id: 'qwen.qwen3-32b-v1:0', label: 'Qwen3 32B (Dense)' },
  { id: 'qwen.qwen3-vl-235b-a22b', label: 'Qwen3 VL 235B A22B' },
  { id: 'qwen.qwen3-coder-480b-a35b-v1:0', label: 'Qwen3 Coder 480B A35B' },
  { id: 'qwen.qwen3-coder-30b-a3b-v1:0', label: 'Qwen3 Coder 30B A3B' },
  { id: '', label: '── GLM (Z.AI) ──', disabled: true },
  { id: 'zai.glm-5', label: 'GLM 5' },
  { id: 'zai.glm-4.7', label: 'GLM 4.7' },
  { id: 'zai.glm-4.7-flash', label: 'GLM 4.7 Flash' },
  { id: '', label: '── MiniMax ──', disabled: true },
  { id: 'minimax.minimax-m2.5', label: 'MiniMax M2.5' },
  { id: 'minimax.minimax-m2.1', label: 'MiniMax M2.1' },
  { id: 'minimax.minimax-m2', label: 'MiniMax M2' },
  { id: '', label: '── Meta Llama ──', disabled: true },
  { id: 'us.meta.llama4-maverick-17b-instruct-v1:0', label: 'Llama 4 Maverick 17B' },
  { id: 'us.meta.llama4-scout-17b-instruct-v1:0', label: 'Llama 4 Scout 17B' },
  { id: 'us.meta.llama3-3-70b-instruct-v1:0', label: 'Llama 3.3 70B Instruct' },
  { id: '', label: '── OpenAI ──', disabled: true },
  { id: 'openai.gpt-oss-120b-1:0', label: 'GPT OSS 120B' },
  { id: 'openai.gpt-oss-20b-1:0', label: 'GPT OSS 20B' },
] as const;

interface MetadataEntry {
  key: string;
  value: string;
}

interface SkillFormData {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string;
  license: string;
  compatibility: string;
  metadata: MetadataEntry[];
}

const emptyForm: SkillFormData = {
  userId: '__global__',
  skillName: '',
  description: '',
  instructions: '',
  allowedTools: 'device_control',
  license: '',
  compatibility: '',
  metadata: [],
};

const AdminConsole: React.FC = () => {
  const [skills, setSkills] = useState<SkillItem[]>([]);
  const [userIds, setUserIds] = useState<string[]>(['__global__']);
  const [selectedUserId, setSelectedUserId] = useState('__global__');
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // Form state
  const [showForm, setShowForm] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [form, setForm] = useState<SkillFormData>(emptyForm);

  // Delete confirmation
  const [deleteTarget, setDeleteTarget] = useState<SkillItem | null>(null);

  // User settings (model ID)
  const [modelId, setModelId] = useState('');
  const [savedModelId, setSavedModelId] = useState('');

  // Tab: 'skills' or 'sessions'
  const [activeTab, setActiveTab] = useState<'skills' | 'sessions'>('skills');

  // Sessions
  const [sessions, setSessions] = useState<SessionInfo[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);

  // File manager (shown when editing a skill)
  const [skillFiles, setSkillFiles] = useState<SkillFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [expandedDirs, setExpandedDirs] = useState<Record<string, boolean>>({
    scripts: true,
    references: true,
    assets: true,
  });

  const loadSkills = useCallback(async () => {
    setIsLoading(true);
    setError('');
    try {
      const items = await listSkills(selectedUserId);
      setSkills(items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setIsLoading(false);
    }
  }, [selectedUserId]);

  const loadSettings = useCallback(async () => {
    try {
      const s = await getSettings(selectedUserId);
      setModelId(s.modelId || '');
      setSavedModelId(s.modelId || '');
    } catch {
      setModelId('');
      setSavedModelId('');
    }
  }, [selectedUserId]);

  const loadSessions = useCallback(async () => {
    setSessionsLoading(true);
    try {
      const items = await listSessions();
      setSessions(items);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSessionsLoading(false);
    }
  }, []);

  const loadSkillFiles = useCallback(async (userId: string, skillName: string) => {
    setFilesLoading(true);
    try {
      const files = await listSkillFiles(userId, skillName);
      setSkillFiles(files);
    } catch {
      setSkillFiles([]);
    } finally {
      setFilesLoading(false);
    }
  }, []);

  const handleFileUpload = async (directory: string, file: File) => {
    if (!isEditing) return;
    setUploading(true);
    clearMessages();
    try {
      const { uploadUrl } = await getUploadUrl(
        form.userId,
        form.skillName,
        directory,
        file.name,
        file.type || 'application/octet-stream'
      );
      await uploadSkillFile(uploadUrl, file);
      setSuccess(`File "${file.name}" uploaded to ${directory}/`);
      loadSkillFiles(form.userId, form.skillName);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setUploading(false);
    }
  };

  const handleFileDownload = async (filePath: string) => {
    clearMessages();
    try {
      const url = await getDownloadUrl(form.userId, form.skillName, filePath);
      window.open(url, '_blank');
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleFileDelete = async (filePath: string) => {
    clearMessages();
    try {
      await deleteSkillFile(form.userId, form.skillName, filePath);
      setSuccess(`File "${filePath}" deleted.`);
      loadSkillFiles(form.userId, form.skillName);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleStopSession = async (sessionId: string) => {
    clearMessages();
    try {
      await stopSession(sessionId);
      setSuccess(`Stop requested for session "${sessionId}".`);
      loadSessions();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleSaveSettings = async () => {
    clearMessages();
    try {
      await updateSettings(selectedUserId, { modelId });
      setSavedModelId(modelId);
      setSuccess(`Model ID updated for ${selectedUserId === '__global__' ? 'default (all users)' : selectedUserId}.`);
    } catch (err: any) {
      setError(err.message);
    }
  };

  const loadUsers = useCallback(async () => {
    try {
      const ids = await listUsers();
      setUserIds(ids.length > 0 ? ids : ['__global__']);
    } catch {
      // Ignore — user list is optional
    }
  }, []);

  useEffect(() => {
    loadSkills();
    loadSettings();
  }, [loadSkills, loadSettings]);

  useEffect(() => {
    loadUsers();
  }, [loadUsers]);

  useEffect(() => {
    if (activeTab === 'sessions') {
      loadSessions();
    }
  }, [activeTab, loadSessions]);

  const clearMessages = () => {
    setError('');
    setSuccess('');
  };

  const handleCreate = () => {
    clearMessages();
    setForm({ ...emptyForm, userId: selectedUserId });
    setIsEditing(false);
    setShowForm(true);
  };

  const handleEdit = (skill: SkillItem) => {
    clearMessages();
    const metadataEntries: MetadataEntry[] = skill.metadata
      ? Object.entries(skill.metadata).map(([key, value]) => ({ key, value }))
      : [];
    setForm({
      userId: skill.userId,
      skillName: skill.skillName,
      description: skill.description,
      instructions: skill.instructions,
      allowedTools: (skill.allowedTools || []).join(', '),
      license: skill.license || '',
      compatibility: skill.compatibility || '',
      metadata: metadataEntries,
    });
    setIsEditing(true);
    setShowForm(true);
    loadSkillFiles(skill.userId, skill.skillName);
  };

  const handleCancel = () => {
    setShowForm(false);
    setForm(emptyForm);
    setSkillFiles([]);
  };

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    clearMessages();

    if (!isEditing && !SKILL_NAME_RE.test(form.skillName)) {
      setError(
        'Invalid skill name. Use 1-64 lowercase alphanumeric characters and hyphens. No leading/trailing/consecutive hyphens.'
      );
      return;
    }

    if (!form.description.trim()) {
      setError('Description is required.');
      return;
    }

    const allowedTools = form.allowedTools
      .split(',')
      .map((t) => t.trim())
      .filter(Boolean);

    // Convert metadata entries to object, filtering empty keys
    const metadata: Record<string, string> = {};
    for (const entry of form.metadata) {
      if (entry.key.trim()) {
        metadata[entry.key.trim()] = entry.value;
      }
    }

    try {
      if (isEditing) {
        await updateSkill(form.userId, form.skillName, {
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license,
          compatibility: form.compatibility,
          metadata,
        });
        setSuccess(`Skill "${form.skillName}" updated.`);
      } else {
        const input: SkillInput = {
          userId: form.userId,
          skillName: form.skillName,
          description: form.description,
          instructions: form.instructions,
          allowedTools,
          license: form.license || undefined,
          compatibility: form.compatibility || undefined,
          metadata: Object.keys(metadata).length > 0 ? metadata : undefined,
        };
        await createSkill(input);
        setSuccess(`Skill "${form.skillName}" created.`);
      }
      setShowForm(false);
      setForm(emptyForm);
      setSkillFiles([]);
      loadSkills();
      loadUsers();
    } catch (err: any) {
      setError(err.message);
    }
  };

  const handleDelete = async () => {
    if (!deleteTarget) return;
    clearMessages();
    try {
      await deleteSkill(deleteTarget.userId, deleteTarget.skillName);
      setSuccess(`Skill "${deleteTarget.skillName}" deleted.`);
      setDeleteTarget(null);
      loadSkills();
      loadUsers();
    } catch (err: any) {
      setError(err.message);
      setDeleteTarget(null);
    }
  };

  const handleAddUserScope = () => {
    const newUser = prompt('Enter user ID (Cognito username or sub):');
    if (newUser && newUser.trim()) {
      const trimmed = newUser.trim();
      if (!userIds.includes(trimmed)) {
        setUserIds((prev) => [...prev, trimmed]);
      }
      setSelectedUserId(trimmed);
    }
  };

  return (
    <div className="admin-console">
      {/* Tabs */}
      <div className="tab-bar">
        <button
          className={`tab ${activeTab === 'skills' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('skills')}
        >
          Skills
        </button>
        <button
          className={`tab ${activeTab === 'sessions' ? 'tab-active' : ''}`}
          onClick={() => setActiveTab('sessions')}
        >
          Sessions
        </button>
      </div>

      {activeTab === 'skills' && (
      <>
      {/* Toolbar */}
      <div className="toolbar">
        <div className="toolbar-left">
          <label className="toolbar-label">User Scope:</label>
          <select
            className="toolbar-select"
            value={selectedUserId}
            onChange={(e) => setSelectedUserId(e.target.value)}
          >
            {userIds.map((id) => (
              <option key={id} value={id}>
                {id === '__global__' ? 'Global (all users)' : id}
              </option>
            ))}
          </select>
          <button className="btn btn-secondary btn-sm" onClick={handleAddUserScope}>
            + User
          </button>
        </div>
        <button className="btn btn-primary" onClick={handleCreate}>
          + Create Skill
        </button>
      </div>

      {/* User Settings — Model ID */}
      <div className="settings-panel">
        <div className="settings-row">
          <label className="toolbar-label">
            {selectedUserId === '__global__' ? 'Default Model:' : 'Model:'}
          </label>
          <select
            className="settings-select"
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
          >
            <option value="">-- Not set (use default) --</option>
            {AVAILABLE_MODELS.map((m, i) =>
              (m as any).disabled ? (
                <option key={i} disabled>{m.label}</option>
              ) : (
                <option key={m.id} value={m.id}>{m.label}</option>
              )
            )}
          </select>
          <button
            className="btn btn-primary btn-sm"
            onClick={handleSaveSettings}
            disabled={modelId === savedModelId}
          >
            Save
          </button>
          {selectedUserId === '__global__' && (
            <span className="settings-hint">Default for users without a per-user model</span>
          )}
        </div>
      </div>

      {/* Messages */}
      {error && <div className="alert alert-error">{error}</div>}
      {success && <div className="alert alert-success">{success}</div>}

      {/* Delete confirmation modal */}
      {deleteTarget && (
        <div className="modal-overlay" onClick={() => setDeleteTarget(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <h3>Delete Skill</h3>
            <p>
              Are you sure you want to delete <strong>{deleteTarget.skillName}</strong> for
              user scope <strong>{deleteTarget.userId}</strong>?
            </p>
            <div className="modal-actions">
              <button className="btn btn-secondary" onClick={() => setDeleteTarget(null)}>
                Cancel
              </button>
              <button className="btn btn-danger" onClick={handleDelete}>
                Delete
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Skill Form */}
      {showForm && (
        <div className="skill-form-container">
          <h3>{isEditing ? 'Edit Skill' : 'Create Skill'}</h3>
          <form onSubmit={handleSubmit}>
            {/* Required fields */}
            <div className="form-row">
              <div className="form-group">
                <label>User Scope</label>
                <input
                  type="text"
                  value={form.userId}
                  onChange={(e) => setForm({ ...form, userId: e.target.value })}
                  disabled={isEditing}
                  placeholder="__global__"
                />
              </div>
              <div className="form-group">
                <label>Skill Name</label>
                <input
                  type="text"
                  value={form.skillName}
                  onChange={(e) =>
                    setForm({ ...form, skillName: e.target.value.toLowerCase() })
                  }
                  disabled={isEditing}
                  placeholder="my-custom-skill"
                />
              </div>
            </div>
            <div className="form-group">
              <label>Description <span className="field-required">*</span></label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="What this skill does and when to use it..."
                maxLength={1024}
              />
            </div>
            <div className="form-group">
              <label>Allowed Tools (space-separated)</label>
              <input
                type="text"
                value={form.allowedTools}
                onChange={(e) => setForm({ ...form, allowedTools: e.target.value })}
                placeholder="device_control"
              />
            </div>

            {/* Optional spec fields */}
            <div className="form-section-label">Optional Fields</div>
            <div className="form-row">
              <div className="form-group">
                <label>License</label>
                <input
                  type="text"
                  value={form.license}
                  onChange={(e) => setForm({ ...form, license: e.target.value })}
                  placeholder="e.g. Apache-2.0"
                />
              </div>
              <div className="form-group">
                <label>Compatibility</label>
                <input
                  type="text"
                  value={form.compatibility}
                  onChange={(e) => setForm({ ...form, compatibility: e.target.value })}
                  placeholder="e.g. Requires Python 3.12+"
                  maxLength={500}
                />
              </div>
            </div>

            {/* Metadata key-value editor */}
            <div className="form-group">
              <label>Metadata</label>
              <div className="metadata-editor">
                {form.metadata.map((entry, i) => (
                  <div key={i} className="metadata-row">
                    <input
                      type="text"
                      className="metadata-key"
                      value={entry.key}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], key: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder="key"
                    />
                    <input
                      type="text"
                      className="metadata-value"
                      value={entry.value}
                      onChange={(e) => {
                        const updated = [...form.metadata];
                        updated[i] = { ...updated[i], value: e.target.value };
                        setForm({ ...form, metadata: updated });
                      }}
                      placeholder="value"
                    />
                    <button
                      type="button"
                      className="btn btn-sm btn-danger metadata-remove"
                      onClick={() => {
                        const updated = form.metadata.filter((_, idx) => idx !== i);
                        setForm({ ...form, metadata: updated });
                      }}
                    >
                      Remove
                    </button>
                  </div>
                ))}
                <button
                  type="button"
                  className="btn btn-sm btn-secondary"
                  onClick={() =>
                    setForm({
                      ...form,
                      metadata: [...form.metadata, { key: '', value: '' }],
                    })
                  }
                >
                  + Add Entry
                </button>
              </div>
            </div>

            {/* Instructions */}
            <div className="form-group">
              <label>Instructions (Markdown)</label>
              <textarea
                className="instructions-textarea"
                value={form.instructions}
                onChange={(e) => setForm({ ...form, instructions: e.target.value })}
                placeholder="# Skill Instructions&#10;&#10;Detailed instructions for the agent..."
                rows={12}
              />
            </div>

            <div className="form-actions">
              <button type="button" className="btn btn-secondary" onClick={handleCancel}>
                Cancel
              </button>
              <button type="submit" className="btn btn-primary">
                {isEditing ? 'Save Changes' : 'Create Skill'}
              </button>
            </div>
          </form>

          {/* File Manager (only when editing) */}
          {isEditing && (
            <div className="file-manager">
              <h4>Skill Files</h4>
              <p className="file-manager-hint">
                Upload scripts, reference docs, and assets for this skill.
              </p>
              {filesLoading ? (
                <div className="loading">Loading files...</div>
              ) : (
                ['scripts', 'references', 'assets'].map((dir) => {
                  const dirFiles = skillFiles.filter((f) => f.path.startsWith(dir + '/'));
                  return (
                    <div key={dir} className="file-dir-section">
                      <button
                        type="button"
                        className="file-dir-header"
                        onClick={() =>
                          setExpandedDirs((prev) => ({ ...prev, [dir]: !prev[dir] }))
                        }
                      >
                        <span className="file-dir-arrow">
                          {expandedDirs[dir] ? '\u25BE' : '\u25B8'}
                        </span>
                        <span className="file-dir-name">{dir}/</span>
                        <span className="file-dir-count">
                          {dirFiles.length} file{dirFiles.length !== 1 ? 's' : ''}
                        </span>
                      </button>
                      {expandedDirs[dir] && (
                        <div className="file-dir-body">
                          {dirFiles.length > 0 && (
                            <table className="file-table">
                              <thead>
                                <tr>
                                  <th>Name</th>
                                  <th>Size</th>
                                  <th>Modified</th>
                                  <th>Actions</th>
                                </tr>
                              </thead>
                              <tbody>
                                {dirFiles.map((f) => {
                                  const name = f.path.split('/').pop() || f.path;
                                  return (
                                    <tr key={f.path}>
                                      <td className="cell-name">{name}</td>
                                      <td className="cell-date">
                                        {f.size < 1024
                                          ? `${f.size} B`
                                          : `${(f.size / 1024).toFixed(1)} KB`}
                                      </td>
                                      <td className="cell-date">
                                        {new Date(f.lastModified).toLocaleDateString()}
                                      </td>
                                      <td className="cell-actions">
                                        <button
                                          type="button"
                                          className="btn btn-sm btn-secondary"
                                          onClick={() => handleFileDownload(f.path)}
                                        >
                                          Download
                                        </button>
                                        <button
                                          type="button"
                                          className="btn btn-sm btn-danger"
                                          onClick={() => handleFileDelete(f.path)}
                                        >
                                          Delete
                                        </button>
                                      </td>
                                    </tr>
                                  );
                                })}
                              </tbody>
                            </table>
                          )}
                          <label className="file-upload-btn btn btn-sm btn-secondary">
                            {uploading ? 'Uploading...' : `Upload to ${dir}/`}
                            <input
                              type="file"
                              hidden
                              disabled={uploading}
                              onChange={(e) => {
                                const file = e.target.files?.[0];
                                if (file) {
                                  handleFileUpload(dir, file);
                                  e.target.value = '';
                                }
                              }}
                            />
                          </label>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>
          )}
        </div>
      )}

      {/* Skills Table */}
      {!showForm && (
        <div className="skills-table-container">
          {isLoading ? (
            <div className="loading">Loading skills...</div>
          ) : skills.length === 0 ? (
            <div className="empty-state">
              <p>No skills found for this user scope.</p>
              <p className="empty-hint">Click "Create Skill" to add one.</p>
            </div>
          ) : (
            <table className="skills-table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Description</th>
                  <th>Tools</th>
                  <th>Updated</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {skills.map((skill) => (
                  <tr key={`${skill.userId}:${skill.skillName}`}>
                    <td className="cell-name">{skill.skillName}</td>
                    <td className="cell-desc">{skill.description}</td>
                    <td className="cell-tools">
                      {(skill.allowedTools || []).join(', ') || '-'}
                    </td>
                    <td className="cell-date">
                      {skill.updatedAt
                        ? new Date(skill.updatedAt).toLocaleDateString()
                        : '-'}
                    </td>
                    <td className="cell-actions">
                      <button className="btn btn-sm btn-secondary" onClick={() => handleEdit(skill)}>
                        Edit
                      </button>
                      <button
                        className="btn btn-sm btn-danger"
                        onClick={() => setDeleteTarget(skill)}
                      >
                        Delete
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
      </>
      )}

      {/* Sessions Tab */}
      {activeTab === 'sessions' && (
        <div className="sessions-section">
          {/* Messages */}
          {error && <div className="alert alert-error">{error}</div>}
          {success && <div className="alert alert-success">{success}</div>}

          <div className="toolbar">
            <div className="toolbar-left">
              <span className="toolbar-label">User Runtime Sessions</span>
            </div>
            <button className="btn btn-secondary btn-sm" onClick={loadSessions}>
              Refresh
            </button>
          </div>

          <div className="skills-table-container">
            {sessionsLoading ? (
              <div className="loading">Loading sessions...</div>
            ) : sessions.length === 0 ? (
              <div className="empty-state">
                <p>No sessions recorded yet.</p>
                <p className="empty-hint">Sessions appear after users interact with the chatbot.</p>
              </div>
            ) : (
              <table className="skills-table">
                <thead>
                  <tr>
                    <th>User ID</th>
                    <th>Session ID</th>
                    <th>Last Active</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {sessions.map((s) => (
                    <tr key={s.sessionId}>
                      <td className="cell-name" title={s.userId}>
                        {s.userId.length > 24 ? s.userId.slice(0, 24) + '...' : s.userId}
                      </td>
                      <td className="cell-tools" title={s.sessionId}>
                        {s.sessionId.length > 36 ? s.sessionId.slice(0, 36) + '...' : s.sessionId}
                      </td>
                      <td className="cell-date">
                        {s.lastActiveAt ? new Date(s.lastActiveAt).toLocaleString() : '-'}
                      </td>
                      <td className="cell-actions">
                        <button
                          className="btn btn-sm btn-danger"
                          onClick={() => handleStopSession(s.sessionId)}
                        >
                          Stop
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default AdminConsole;
