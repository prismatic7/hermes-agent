import { atom } from 'nanostores'

import type { SidebarProjectTree } from '@/app/chat/sidebar/workspace-groups'
import { persistString, storedString } from '@/lib/storage'
import { activeGateway, ensureActiveGatewayOpen } from '@/store/gateway'
import type { ProjectInfo, ProjectsPayload } from '@/types/hermes'

// First-class, per-profile Projects (named, multi-folder workspaces). State is
// served by the live gateway's `projects.*` JSON-RPC methods, which wrap the
// per-profile projects.db store. The sidebar groups sessions by project folder
// membership; these atoms are the renderer's cached view.

export const $projects = atom<ProjectInfo[]>([])
export const $activeProjectId = atom<null | string>(null)

// The authoritative project -> repo -> lane tree (overview), served by
// `projects.tree`. Lanes carry counts + structure; per-project session rows are
// fetched lazily on drill-in via `fetchProjectSessions`. This is the single
// source of project membership — the desktop no longer derives it.
export const $projectTree = atom<SidebarProjectTree[]>([])
export const $projectTreeLoading = atom(false)
// Session ids claimed by any project, so the flat Recents list can exclude them
// (one membership set, straight from the backend tree).
export const $scopedSessionIds = atom<Set<string>>(new Set())
// True while the disk scan is in flight (drives the "finding repos" hint).
export const $reposScanning = atom(false)

// ── Project scope (the "you're inside a project" view, mirroring profile scope)─
// The sidebar's grouped view is a project switcher: ALL_PROJECTS shows the
// project overview (a list you drill into), and a concrete id means you've
// "entered" that project so only its worktrees/branches/sessions show. This is
// pure view state (localStorage), distinct from the durable active-project
// pointer in projects.db — though entering a project also makes it active so new
// chats land there, exactly as selecting a profile does.
export const ALL_PROJECTS = '__all_projects__'

const PROJECT_SCOPE_KEY = 'hermes.desktop.projectScope'

export const $projectScope = atom<string>(storedString(PROJECT_SCOPE_KEY) || ALL_PROJECTS)

$projectScope.subscribe(value => persistString(PROJECT_SCOPE_KEY, value || ALL_PROJECTS))

// Enter a project: scope the sidebar to it and make it the active project
// (best-effort — the durable pointer is nice-to-have, the view scope is the
// point). Never opens a session.
export function enterProject(id: string): void {
  $projectScope.set(id)

  // Only explicit, persisted projects (ids are `p_<hex>`) become active. Auto
  // projects (ids are filesystem paths) and the "No project" bucket have no
  // durable row to pin, so they're view-scope only.
  if (id.startsWith('p_')) {
    void setActiveProject(id).catch(() => undefined)
  }
}

export function exitProjectScope(): void {
  $projectScope.set(ALL_PROJECTS)
}

// Issue a request on whichever gateway is currently active, reconnecting once
// if the socket dropped. Projects are per-profile, so they intentionally follow
// the active gateway just like the session list does.
async function gatewayRequest<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
  let gateway = activeGateway()

  if (!gateway || gateway.connectionState !== 'open') {
    gateway = await ensureActiveGatewayOpen()
  }

  if (!gateway) {
    throw new Error('Hermes gateway is not connected')
  }

  return gateway.request<T>(method, params)
}

function applyPayload(payload: ProjectsPayload): void {
  $projects.set(payload.projects ?? [])
  $activeProjectId.set(payload.active_id ?? null)
}

// Pull the full project list + active pointer. Best-effort: a failure (gateway
// not up yet) leaves the cached atoms intact so the sidebar doesn't flicker.
export async function refreshProjects(): Promise<void> {
  try {
    applyPayload(await gatewayRequest<ProjectsPayload>('projects.list'))
  } catch {
    // Backend may not be ready; keep the last known list.
  }
}

interface ProjectTreePayload {
  projects: SidebarProjectTree[]
  active_id: null | string
  scoped_session_ids: string[]
}

// Pull the authoritative project tree (overview structure + counts + preview
// sessions + the scoped-session-id set). Best-effort: a failure leaves the
// cached tree intact so the sidebar doesn't flicker.
export async function refreshProjectTree(): Promise<void> {
  $projectTreeLoading.set(true)
  try {
    const res = await gatewayRequest<ProjectTreePayload>('projects.tree', { preview_limit: 3 })
    $projectTree.set(res.projects ?? [])
    $scopedSessionIds.set(new Set(res.scoped_session_ids ?? []))
    $activeProjectId.set(res.active_id ?? null)
  } catch {
    // Backend may not be ready; keep the last known tree.
  } finally {
    $projectTreeLoading.set(false)
  }
}

// Fully hydrated lanes (repo -> lane -> session rows) for one project, fetched
// when the user enters it. Same backend grouping as `projects.tree`, so ids and
// membership match exactly.
export async function fetchProjectSessions(projectId: string): Promise<SidebarProjectTree | null> {
  try {
    const res = await gatewayRequest<{ project: SidebarProjectTree | null }>('projects.project_sessions', {
      project_id: projectId
    })

    return res.project ?? null
  } catch {
    return null
  }
}

// One filesystem scan per app run: the heavy disk walk happens once, the result
// is cached in the backend, and later opens read the cache. Desktop-only (needs
// the native crawler); elsewhere discovery falls back to session-derived repos.
let didScanRepos = false

export async function scanAndRecordRepos(force = false): Promise<void> {
  const scan = window.hermesDesktop?.git?.scanRepos

  if (!scan || (didScanRepos && !force)) {
    return
  }

  didScanRepos = true
  $reposScanning.set(true)

  try {
    const repos = await scan([])
    await gatewayRequest('projects.record_repos', { repos })
    // The disk scan may surface new zero-session repos; refold them into the tree.
    await refreshProjectTree()
  } catch {
    didScanRepos = false // let a later open retry a failed scan
  } finally {
    $reposScanning.set(false)
  }
}

export interface CreateProjectInput {
  name: string
  folders?: string[]
  primaryPath?: string
  slug?: string
  description?: string
  icon?: string
  color?: string
  boardSlug?: string
  use?: boolean
}

export async function createProject(input: CreateProjectInput): Promise<ProjectInfo | null> {
  const res = await gatewayRequest<{ project: ProjectInfo | null }>('projects.create', {
    name: input.name,
    folders: input.folders ?? [],
    primary_path: input.primaryPath,
    slug: input.slug,
    description: input.description,
    icon: input.icon,
    color: input.color,
    board_slug: input.boardSlug,
    use: input.use ?? false
  })

  await refreshProjects()

  return res.project
}

export async function renameProject(id: string, name: string): Promise<void> {
  await gatewayRequest('projects.update', { id, name })
  await refreshProjects()
}

export async function addProjectFolder(
  id: string,
  path: string,
  opts: { label?: string; isPrimary?: boolean } = {}
): Promise<void> {
  await gatewayRequest('projects.add_folder', {
    id,
    path,
    label: opts.label,
    is_primary: opts.isPrimary ?? false
  })
  await refreshProjects()
}

export async function deleteProject(id: string): Promise<void> {
  applyPayload(await gatewayRequest<ProjectsPayload>('projects.delete', { id }))
}

export async function setActiveProject(id: null | string): Promise<void> {
  const res = await gatewayRequest<{ active_id: null | string }>('projects.set_active', { id })
  $activeProjectId.set(res.active_id ?? null)
}

// ── Project management dialog ────────────────────────────────────────────────
// A single dialog mounted in the sidebar reads this atom, so a project node's
// menu can open create / rename / add-folder flows without prop threading
// (mirrors $profileCreateRequest).
export interface ProjectDialogState {
  mode: 'add-folder' | 'create' | 'rename'
  projectId?: string
  name?: string
}

export const $projectDialog = atom<null | ProjectDialogState>(null)

export function openProjectCreate(): void {
  $projectDialog.set({ mode: 'create' })
}

export function openProjectRename(project: { id: string; name: string }): void {
  $projectDialog.set({ mode: 'rename', name: project.name, projectId: project.id })
}

export function openProjectAddFolder(project: { id: string; name: string }): void {
  $projectDialog.set({ mode: 'add-folder', name: project.name, projectId: project.id })
}

export function closeProjectDialog(): void {
  $projectDialog.set(null)
}

// ── Git-driven worktrees ("Start work") ─────────────────────────────────────
// Bumped after a `git worktree add`/`remove` so the sidebar's worktree-list
// probe (useRepoWorktreeMap) refetches and the new/removed lane shows at once,
// instead of waiting for the next scope change.
export const $worktreeRefreshToken = atom(0)
const bumpWorktrees = () => $worktreeRefreshToken.set($worktreeRefreshToken.get() + 1)

// Spin up a fresh worktree the lightest way (`git worktree add -b`) under the
// repo, returning where Hermes should start working. Git is the source of
// truth; the caller starts a session in the returned path.
export async function startWorkInRepo(
  repoPath: string,
  options?: { name?: string; branch?: string }
): Promise<null | { path: string; branch: string }> {
  const git = window.hermesDesktop?.git

  if (!git || !repoPath) {
    return null
  }

  const result = await git.worktreeAdd(repoPath, options)
  bumpWorktrees()

  return { branch: result.branch, path: result.path }
}

export async function removeWorktreePath(repoPath: string, worktreePath: string): Promise<void> {
  const git = window.hermesDesktop?.git

  if (!git) {
    return
  }

  await git.worktreeRemove(repoPath, worktreePath)
  bumpWorktrees()
}

// Reveal a project/worktree path in the OS file manager (git-GUI standard).
export async function revealPath(path: null | string): Promise<void> {
  if (path) {
    await window.hermesDesktop?.revealPath?.(path)
  }
}

// Copy a path to the clipboard (git-GUI standard).
export async function copyPath(path: null | string): Promise<void> {
  if (path) {
    await window.hermesDesktop?.writeClipboard?.(path)
  }
}

// Open the native directory picker (reuses the Electron default-project-dir
// chooser). Returns the chosen absolute path, or null when cancelled.
export async function pickProjectFolder(): Promise<null | string> {
  const pick = window.hermesDesktop?.settings?.pickDefaultProjectDir

  if (!pick) {
    return null
  }

  try {
    const result = await pick()

    return result.canceled ? null : result.dir
  } catch {
    return null
  }
}
