# Git Workflow Guidelines

## Overview
This document describes the best practices for managing code features in our three projects:
- **qadmcli** - AS400/MSSQL database administration CLI
- **gluesync-cli** - GlueSync pipeline management CLI
- **replica-mon** - Replication monitoring and reconciliation tool

## Branch Strategy

### Main Branches
- `main` - Production-ready code, always stable
- `develop` - Integration branch for features (optional for small projects)

### Feature Branches
**Naming Convention:**
```
feature/<description>       # New features
bugfix/<description>        # Bug fixes
enhancement/<description>   # Improvements to existing features
```

**Examples:**
```bash
feature/journal-time-filter
feature/ct-version-range
enhancement/json-output-format
bugfix/credential-validation
```

### Release Tags
**Format:** `v<major>.<minor>.<patch>`
- **Major**: Breaking changes
- **Minor**: New features (backward compatible)
- **Patch**: Bug fixes

**Examples:**
```bash
v0.3.1  # Current qadmcli with MSSQL CT
v0.4.0  # Next release with time filters
v1.0.0  # First stable release
```

## Feature Development Workflow

### 1. Create Feature Branch
```bash
cd ~/_qoder/qadmcli
git checkout main
git pull origin main
git checkout -b feature/journal-time-filter
```

### 2. Make Changes
- Work on small, focused commits
- Use descriptive commit messages (see format below)
- Test thoroughly before committing

### 3. Commit Message Format
```
<type>(<scope>): <subject>

<body>

<footer>
```

**Types:**
- `feat` - New feature
- `fix` - Bug fix
- `enhance` - Enhancement to existing feature
- `docs` - Documentation only
- `test` - Adding/updating tests
- `refactor` - Code refactoring (no functional change)

**Examples:**
```bash
# Good commit messages
feat(journal): add --from-time and --to-time filters

Add time range filtering to journal entries query.
Supports ISO 8601 format: YYYY-MM-DD or YYYY-MM-DD HH:MM:SS

Closes #12

enhance(output): add --summary flag for operation counts

Add summary mode that shows operation counts without 
full entry details. Useful for comparison with MSSQL CT.

fix(mssql): correct env var name MSSQL_USER not MSSQL_USERNAME

Standardize environment variable naming across all projects.
```

### 4. Create Tag for Releases
```bash
# After merging feature to main
git checkout main
git merge feature/journal-time-filter
git tag -a v0.4.0 -m "Add journal time filters and summary output"
git push origin main --tags
```

### 5. Delete Feature Branch
```bash
# After merge
git branch -d feature/journal-time-filter
git push origin --delete feature/journal-time-filter
```

## Best Practices

### Do's ✅
1. **One feature per branch** - Keep changes focused
2. **Commit early, commit often** - Small, logical commits
3. **Test before commit** - Ensure code works
4. **Write descriptive messages** - Explain WHY, not WHAT
5. **Use tags for releases** - Mark stable versions
6. **Pull before push** - Avoid conflicts
7. **Clean up branches** - Delete after merge

### Don'ts ❌
1. **Don't commit to main directly** - Use feature branches
2. **Don't mix features** - One purpose per branch
3. **Don't skip tags** - Tags help track releases
4. **Don't force push** - Unless absolutely necessary
5. **Don't commit secrets** - Use .env files
6. **Don't leave old branches** - Clean up regularly

## Common Workflows

### Adding a New Feature
```bash
# 1. Start from main
git checkout main
git pull origin main

# 2. Create feature branch
git checkout -b feature/comparison-report

# 3. Make changes and commit
git add .
git commit -m "feat(cli): add comparison report command

Add compare command that queries both AS400 journal and 
MSSQL CT, then generates discrepancy report."

# 4. Push branch
git push origin feature/comparison-report

# 5. Test, then merge to main
git checkout main
git merge feature/comparison-report
git tag -a v0.5.0 -m "Add comparison report feature"
git push origin main --tags

# 6. Clean up
git branch -d feature/comparison-report
git push origin --delete feature/comparison-report
```

### Hotfix (Urgent Bug Fix)
```bash
# 1. Create hotfix branch from main
git checkout main
git checkout -b hotfix/credential-leak

# 2. Fix and commit
git add .
git commit -m "fix(security): redact credentials from logs"

# 3. Merge and tag
git checkout main
git merge hotfix/credential-leak
git tag -a v0.3.2 -m "Hotfix: credential leak in logs"
git push origin main --tags
```

## Project-Specific Notes

### qadmcli
- Tag versions when adding new CLI commands
- Document new options in README.md
- Test container build after changes: `podman build -t qadmcli .`

### gluesync-cli
- Tag when API commands change
- Keep proxy/ folder separate from main CLI features

### replica-mon
- Tag when comparison logic changes
- Document new report formats
- Test with real data before tagging

## Private Repository Setup

### Making Repositories Private

All three projects can be set to private on GitHub without affecting Qoder or local development workflow.

#### Steps to Make a Repository Private:

1. **On GitHub:**
   - Navigate to repository → Settings → General
   - Scroll to "Danger Zone"
   - Click "Change visibility" → Select "Make private"
   - Confirm the change

2. **Impact:**
   - ✅ Qoder continues to work normally
   - ✅ All Git operations work (clone, pull, push, branch, merge)
   - ✅ CI/CD workflows continue (with proper tokens)
   - ❌ Public can no longer see or clone the repository
   - ❌ Public links to code will return 404

---

### Authentication Methods for Private Repos

Qoder uses your system's Git configuration. Choose one authentication method:

#### **Option 1: SSH Keys (Recommended)**

**Advantages:**
- ✅ Most secure
- ✅ No repeated authentication
- ✅ Works with all Git operations
- ✅ Best for automation and scripts

**Setup:**

```bash
# 1. Generate SSH key (if not exists)
ssh-keygen -t ed25519 -C "your_email@example.com"
# Press Enter to accept default location
# Optionally set a passphrase for extra security

# 2. Copy public key
cat ~/.ssh/id_ed25519.pub
# Copy the entire output

# 3. Add to GitHub:
#    - Go to GitHub → Settings → SSH and GPG keys
#    - Click "New SSH key"
#    - Paste your public key
#    - Give it a descriptive title (e.g., "apimdev2 workstation")

# 4. Test connection
ssh -T git@github.com
# Expected: "Hi username! You've successfully authenticated..."
```

**Update Remote URLs to SSH:**

```bash
# For qadmcli
cd ~/_qoder/qadmcli
git remote set-url origin git@github.com:pnandasa-aide/qadmcli.git

# For gluesync-cli
cd ~/_qoder/gluesync-cli
git remote set-url origin git@github.com:pnandasa-aide/gluesync-cli.git

# For replica-mon
cd ~/_qoder/replica-mon
git remote set-url origin git@github.com:pnandasa-aide/replica-mon.git

# Verify
git remote -v
# Should show: git@github.com:pnandasa-aide/<repo>.git
```

---

#### **Option 2: Personal Access Token (PAT)**

**When to use:** If SSH is not available or preferred

**Setup:**

```bash
# 1. Generate token on GitHub:
#    - GitHub → Settings → Developer settings → Personal access tokens → Tokens (classic)
#    - Click "Generate new token (classic)"
#    - Select scopes: repo, workflow (minimum)
#    - Generate and copy the token (save it securely!)

# 2. Configure Git credential helper
git config --global credential.helper store

# 3. Clone or push (will prompt for credentials)
git clone https://github.com/pnandasa-aide/qadmcli.git
# Username: your-github-username
# Password: paste-your-token-here (NOT your GitHub password)

# Or cache credentials for 1 hour
git config --global credential.helper 'cache --timeout=3600'
```

**⚠️ Security Warning:**
- Tokens are stored in plain text with `store` helper
- Use SSH if possible for better security
- Rotate tokens regularly

---

#### **Option 3: GitHub CLI**

**Setup:**

```bash
# 1. Install GitHub CLI
sudo apt install gh

# 2. Authenticate
gh auth login
# Follow prompts:
# - Choose GitHub.com
# - Choose HTTPS or SSH protocol
# - Complete browser authentication

# 3. Clone private repo
gh repo clone pnandasa-aide/qadmcli ~/_qoder/qadmcli
```

**Benefits:**
- Integrated with GitHub workflows
- Can create PRs, issues, manage repos from CLI
- Handles authentication automatically

---

### Verifying Private Repo Access

After setting up authentication:

```bash
# Test pull
cd ~/_qoder/qadmcli
git pull origin main

# Test push
git checkout -b test-private-access
git commit --allow-empty -m "test: verify private repo access"
git push origin test-private-access

# Clean up
git checkout main
git branch -d test-private-access
git push origin --delete test-private-access
```

**Expected:** All operations complete without authentication prompts

---

### Troubleshooting Private Repo Issues

#### **Issue: Permission denied (publickey)**
```bash
# Check SSH key exists
ls -la ~/.ssh/id_ed25519

# Check SSH agent
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519

# Test SSH connection
ssh -T git@github.com -v
# Look for "Authentication succeeded" in verbose output
```

#### **Issue: Repository not found**
```bash
# Verify remote URL
git remote -v

# Should be SSH format for private repos:
# origin  git@github.com:pnandasa-aide/qadmcli.git (fetch)
# origin  git@github.com:pnandasa-aide/qadmcli.git (push)

# If showing HTTPS, switch to SSH:
git remote set-url origin git@github.com:pnandasa-aide/qadmcli.git
```

#### **Issue: Authentication required every time**
```bash
# For HTTPS: Configure credential helper
git config --global credential.helper store

# For SSH: Ensure key is in agent
ssh-add ~/.ssh/id_ed25519

# Make persistent (add to ~/.bashrc):
echo 'eval "$(ssh-agent -s)"' >> ~/.bashrc
echo 'ssh-add ~/.ssh/id_ed25519' >> ~/.bashrc
```

---

### Multi-User Collaboration with Private Repos

When multiple developers need access:

1. **Add collaborators on GitHub:**
   - Repo → Settings → Collaborators and teams
   - Add users by GitHub username
   - Set permission level (Read, Write, Admin)

2. **Each user sets up authentication:**
   - Generate their own SSH key
   - Add to their GitHub account
   - Clone using SSH URL

3. **Shared workflows:**
   - Use feature branches (as documented above)
   - Create Pull Requests for code review
   - Protect main branch (Settings → Branches → Add rule)

---

### Best Practices for Private Repos

#### **Do's ✅**
1. Use SSH keys for authentication
2. Keep `.env` files in `.gitignore`
3. Rotate access tokens regularly
4. Review collaborator access periodically
5. Use branch protection rules
6. Enable two-factor authentication on GitHub
7. Audit repository access logs

#### **Don'ts ❌**
1. Don't commit secrets or credentials
2. Don't share SSH private keys
3. Don't use personal tokens in scripts
4. Don't make repo public if it contains sensitive data
5. Don't bypass branch protection
6. Don't leave inactive collaborators with access

---

### Migration Checklist: Public → Private

```bash
# □ 1. Update repository visibility on GitHub
# □ 2. Generate SSH key (if not exists)
# □ 3. Add SSH key to GitHub account
# □ 4. Test SSH connection: ssh -T git@github.com
# □ 5. Update all local clones to use SSH:
#      git remote set-url origin git@github.com:pnandasa-aide/<repo>.git
# □ 6. Test pull: git pull origin main
# □ 7. Test push: git push origin main
# □ 8. Verify Qoder still works
# □ 9. Update any CI/CD tokens if needed
# □ 10. Remove inactive collaborators
# □ 11. Enable branch protection
# □ 12. Document access for team members
```

---

### Qoder Compatibility

**Qoder works seamlessly with private repositories:**
- ✅ All IDE features (code intelligence, symbols, references)
- ✅ Git integration (commit, push, pull, branch)
- ✅ Remote development via SSH
- ✅ Terminal access
- ✅ File operations
- ✅ Search and replace

**No configuration changes needed in Qoder** - it uses your system's Git setup automatically.

## Quick Reference

```bash
# View branches
git branch -a

# View tags
git tag -l

# View commit history
git log --oneline --graph --all

# View changes before commit
git status
git diff

# Undo last commit (keep changes)
git reset --soft HEAD~1

# View tag details
git show v0.4.0

# Switch to SSH (for private repos)
git remote set-url origin git@github.com:pnandasa-aide/<repo>.git

# Test SSH connection
ssh -T git@github.com

# View authentication status
git config --global credential.helper
```
