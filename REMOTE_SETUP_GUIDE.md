# Qoder Remote IDE Setup Guide

This guide explains how to configure Qoder to work with remote terminal sessions via SSH.

## Overview

Qoder can connect to remote machines using SSH, allowing you to:
- Edit files on remote servers
- Run commands on remote terminals
- Use remote development environments
- Access remote databases and tools

## Prerequisites

1. **Remote machine** accessible via SSH
2. **SSH client** installed on your local machine
3. **SSH server** running on remote machine
4. **Network access** between local and remote machines

## Step 1: Generate SSH Key Pair

On your **local machine**, generate an SSH key pair:

```bash
# Generate Ed25519 key (recommended)
ssh-keygen -t ed25519 -C "your_email@example.com"

# Or generate RSA key (if Ed25519 not supported)
ssh-keygen -t rsa -b 4096 -C "your_email@example.com"
```

**When prompted:**
- Enter file location: Press Enter for default (`~/.ssh/id_ed25519`)
- Enter passphrase: Press Enter for no passphrase (or set one for security)

**Verify key was created:**
```bash
ls -la ~/.ssh/id_ed25519*
# Should show: id_ed25519 (private) and id_ed25519.pub (public)
```

## Step 2: Copy Public Key to Remote Machine

### Option A: Using ssh-copy-id (Easiest)

```bash
ssh-copy-id username@remote_host

# Example:
ssh-copy-id ubuntu@192.168.13.53
```

Enter the remote user's password when prompted.

### Option B: Manual Copy

```bash
# Copy public key content
cat ~/.ssh/id_ed25519.pub

# SSH to remote machine
ssh username@remote_host

# Create .ssh directory if it doesn't exist
mkdir -p ~/.ssh
chmod 700 ~/.ssh

# Add your public key to authorized_keys
echo "YOUR_PUBLIC_KEY_CONTENT" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# Exit remote session
exit
```

## Step 3: Test SSH Connection

```bash
# Test connection
ssh username@remote_host

# Example:
ssh ubuntu@192.168.13.53
```

**If successful:**
- You should be logged in without password prompt
- You should see the remote machine's shell

**If failed:**
- Check SSH service is running: `sudo systemctl status sshd`
- Verify firewall allows port 22: `sudo ufw status`
- Check key permissions: `ls -la ~/.ssh/`

## Step 4: Configure SSH Config File (Optional but Recommended)

On your **local machine**, create/edit SSH config:

```bash
nano ~/.ssh/config
```

Add your remote host configuration:

```
# GlueSync Development Server
Host gluesync-dev
    HostName 192.168.13.53
    User ubuntu
    Port 22
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 60
    ServerAliveCountMax 3

# AS400 Management Server
Host as400-mgmt
    HostName 192.168.13.62
    User admin
    Port 22
    IdentityFile ~/.ssh/id_ed25519
```

**Test with alias:**
```bash
ssh gluesync-dev
```

## Step 5: Configure Qoder for Remote Access

### In Qoder IDE:

1. **Open Qoder Settings**
   - Click gear icon ⚙️ in bottom-left
   - Or press `Ctrl+,` (Linux/Windows) / `Cmd+,` (Mac)

2. **Navigate to Remote Settings**
   - Search for "remote" or "SSH"
   - Look for "Remote - SSH" extension settings

3. **Add Remote Host**
   - Open Command Palette: `Ctrl+Shift+P` / `Cmd+Shift+P`
   - Type: `Remote-SSH: Connect to Host...`
   - Select your host from config OR enter manually:
     ```
     username@hostname
     # Example: ubuntu@192.168.13.53
     ```

4. **Select Authentication Method**
   - Choose "SSH Key" (recommended)
   - Or "Password" if key-based auth not set up

5. **Open Remote Workspace**
   - After connection, select folder to open:
     ```
     /home/ubuntu/_qoder
     ```

## Step 6: Verify Remote Terminal

Once connected:

1. **Open Terminal in Qoder**
   - Terminal → New Terminal
   - Or press `` Ctrl+` ``

2. **Verify you're on remote machine**
   ```bash
   hostname
   pwd
   whoami
   ```

   **Expected output:**
   ```
   gluesync-dev
   /home/ubuntu/_qoder
   ubuntu
   ```

3. **Test file access**
   ```bash
   ls -la
   cat .env
   ```

## Troubleshooting

### Connection Refused
```bash
# On remote machine, check SSH is running
sudo systemctl status sshd
sudo systemctl start sshd

# Check firewall
sudo ufw allow 22/tcp
```

### Permission Denied (publickey)
```bash
# On local machine, verify key exists
ls -la ~/.ssh/id_ed25519*

# On remote machine, check authorized_keys
cat ~/.ssh/authorized_keys

# Verify permissions
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### Host Key Verification Failed
```bash
# Remove old host key
ssh-keygen -R hostname_or_ip

# Reconnect (will prompt to accept new key)
ssh username@hostname
```

### Connection Timeout
```bash
# Test network connectivity
ping remote_host

# Test SSH port
telnet remote_host 22
# or
nc -zv remote_host 22
```

## Security Best Practices

1. **Use SSH keys, not passwords**
   - More secure
   - Enables automation
   - No password exposure

2. **Set key passphrase** (optional but recommended)
   ```bash
   ssh-keygen -p -f ~/.ssh/id_ed25519
   ```

3. **Use SSH agent for convenience**
   ```bash
   # Start agent
   eval "$(ssh-agent -s)"
   
   # Add key to agent
   ssh-add ~/.ssh/id_ed25519
   
   # List added keys
   ssh-add -l
   ```

4. **Restrict SSH access**
   ```bash
   # On remote machine, edit SSH config
   sudo nano /etc/ssh/sshd_config
   
   # Disable password authentication
   PasswordAuthentication no
   
   # Disable root login
   PermitRootLogin no
   
   # Restart SSH
   sudo systemctl restart sshd
   ```

5. **Use non-standard port** (optional)
   ```bash
   # Change SSH port in /etc/ssh/sshd_config
   Port 2222
   
   # Update SSH config
   Host gluesync-dev
       Port 2222
   ```

## Quick Reference

### Common SSH Commands

```bash
# Generate key
ssh-keygen -t ed25519 -C "comment"

# Copy key to remote
ssh-copy-id user@host

# Connect to remote
ssh user@host

# Connect with specific key
ssh -i ~/.ssh/specific_key user@host

# Run command on remote
ssh user@host "command to run"

# Copy file to remote
scp file.txt user@host:/path/to/destination

# Copy directory to remote
scp -r directory/ user@host:/path/to/destination

# Test connection verbose mode
ssh -v user@host
```

### File Permissions

```bash
# Local machine
chmod 700 ~/.ssh
chmod 600 ~/.ssh/id_ed25519
chmod 644 ~/.ssh/id_ed25519.pub

# Remote machine
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

## Example: Full Setup

```bash
# 1. Generate key
ssh-keygen -t ed25519 -C "dev@gluesync"

# 2. Copy to remote
ssh-copy-id ubuntu@192.168.13.53

# 3. Test connection
ssh ubuntu@192.168.13.53

# 4. Configure SSH alias
cat >> ~/.ssh/config << 'EOF'
Host gluesync-dev
    HostName 192.168.13.53
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
EOF

# 5. Connect with alias
ssh gluesync-dev

# 6. In Qoder, connect to: ubuntu@192.168.13.53
#    Or use alias if configured: gluesync-dev
```

## Next Steps

After setting up remote access:
1. Open your project in Qoder
2. Use remote terminal for development
3. Run commands as if working locally
4. All changes happen on remote machine
5. Git operations work normally

For questions or issues, refer to:
- [OpenSSH Documentation](https://www.openssh.com/manual.html)
- [Qoder Remote Development Docs](https://qoder.com/docs/remote)
