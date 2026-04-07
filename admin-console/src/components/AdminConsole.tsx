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
  SkillItem,
  SkillInput,
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
  { id: '', label: '── Meta Llama ──', disabled: true },
  { id: 'us.meta.llama4-maverick-17b-instruct-v1:0', label: 'Llama 4 Maverick 17B' },
  { id: 'us.meta.llama4-scout-17b-instruct-v1:0', label: 'Llama 4 Scout 17B' },
  { id: 'us.meta.llama3-3-70b-instruct-v1:0', label: 'Llama 3.3 70B Instruct' },
  { id: '', label: '── OpenAI ──', disabled: true },
  { id: 'openai.gpt-oss-120b-1:0', label: 'GPT OSS 120B' },
  { id: 'openai.gpt-oss-20b-1:0', label: 'GPT OSS 20B' },
] as const;

interface SkillFormData {
  userId: string;
  skillName: string;
  description: string;
  instructions: string;
  allowedTools: string;
}

const emptyForm: SkillFormData = {
  userId: '__global__',
  skillName: '',
  description: '',
  instructions: '',
  allowedTools: 'device_control',
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
    setForm({
      userId: skill.userId,
      skillName: skill.skillName,
      description: skill.description,
      instructions: skill.instructions,
      allowedTools: (skill.allowedTools || []).join(', '),
    });
    setIsEditing(true);
    setShowForm(true);
  };

  const handleCancel = () => {
    setShowForm(false);
    setForm(emptyForm);
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

    try {
      if (isEditing) {
        await updateSkill(form.userId, form.skillName, {
          description: form.description,
          instructions: form.instructions,
          allowedTools,
        });
        setSuccess(`Skill "${form.skillName}" updated.`);
      } else {
        const input: SkillInput = {
          userId: form.userId,
          skillName: form.skillName,
          description: form.description,
          instructions: form.instructions,
          allowedTools,
        };
        await createSkill(input);
        setSuccess(`Skill "${form.skillName}" created.`);
      }
      setShowForm(false);
      setForm(emptyForm);
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
              <label>Description</label>
              <input
                type="text"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="What this skill does..."
              />
            </div>
            <div className="form-group">
              <label>Allowed Tools (comma-separated)</label>
              <input
                type="text"
                value={form.allowedTools}
                onChange={(e) => setForm({ ...form, allowedTools: e.target.value })}
                placeholder="device_control"
              />
            </div>
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
