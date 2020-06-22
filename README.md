# esptk

A shell-based & python-based, open source, utility to develop with the linux, specially for espressif platform.

esptk was started by ustccw (@[ustccw](https://github.com/ustccw/)) as an unofficial community project.  
Current primary maintainer is ustccw (@[ustccw](https://github.com/ustccw/)).

esptk is Free Software under a MIT license.

# Development Mode Installation

Development mode allows you to run the latest development version from this repository.

### Temporary installation to shell terminal
```shell
$ git clone https://github.com/ustccw/esptk.git
$ cd esptk
$ git submodule update --init --recursive
$ export PATH=./bin/:$PATH
```

### Permanent installation to shell terminal
```shell
$ git clone https://github.com/ustccw/esptk.git
$ cd esptk
$ git submodule update --init --recursive
$ TK_PATH=`realpath ./bin`
$ echo "# add by esptk" >> ~/.bashrc
$ echo "export PATH=$TK_PATH:\$PATH" >> ~/.bashrc
$ echo "" >> ~/.bashrc
$ source ~/.bashrc
```

# Usage

Use `esptk` to see the version information.  
Use `esptk -h` to see a summary of all available commands and command brief introduction.  

To see all options for a particular command, append `-h` to the command name.  
such as `ef -h`.

# Troubleshoot & Feature request
Please raise a [issue](https://github.com/ustccw/esptk/issues) if any question.  
Any [Pull request](https://github.com/ustccw/esptk/pulls) is welcomed if you have a good idea.  
