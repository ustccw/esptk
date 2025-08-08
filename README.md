# esptk

A shell-based & python-based, open source, utility to develop with the linux, specially for espressif platform.

esptk was started by ustccw (@[ustccw](https://github.com/ustccw/)) as an unofficial community project.  
Current primary maintainer is ustccw (@[ustccw](https://github.com/ustccw/)).

esptk is Free Software under a MIT license.

# Installation

```
source install.sh
```

# Environment Setup

After installation, add `esptk/bin` to your PATH permanently by adding the following line to your shell configuration file:

**For Bash users (~/.bashrc):**
```bash
echo 'export PATH=/path/to/esptk/bin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

**For Zsh users (~/.zshrc):**
```bash
echo 'export PATH=/path/to/esptk/bin:$PATH' >> ~/.zshrc
source ~/.zshrc
```

**Note:** Replace `/path/to/esptk` with the actual path to your esptk installation directory.

Alternatively, you can add the PATH manually in each terminal session:
```bash
export PATH=/path/to/esptk/bin:$PATH
```

# Usage

Use `esptk` to see the version information.  
Use `esptk -h` to see a summary of all available commands and command brief introduction.  

To see all options for a particular command, append `-h` to the command name.  
such as `ef -h`.
