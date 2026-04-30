#!/bin/bash

# 1. Update and install basic build tools
echo "Updating system and installing build essentials..."
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential cmake git pkg-config wget

# 2. Install NVIDIA Drivers
# This installs the recommended proprietary driver for your specific card
echo "Installing NVIDIA drivers..."
sudo ubuntu-drivers autoinstall

# 3. Install Vulkan SDK and Dependencies
# Kompute requires the Vulkan loader and headers
echo "Installing Vulkan dependencies..."
sudo apt install -y libvulkan-dev vulkan-tools libglfw3-dev glslang-tools

# 4. Install Python and Pip (for the Python wrapper 'kp')
echo "Setting up Python environment..."
sudo apt install -y python3-pip python3-dev
pip3 install --upgrade pip

# 5. Clone and Build Kompute from Source
# We build with Python bindings enabled
echo "Cloning Kompute repository..."
git clone https://github.com/KomputeProject/kompute.git
cd kompute
mkdir build && cd build

echo "Configuring and building Kompute..."
cmake .. \
    -DKOMPUTE_OPT_BUILD_PYTHON=ON \
    -DKOMPUTE_OPT_INSTALL=ON \
    -DCMAKE_BUILD_TYPE=Release

# Use all available cores for a faster build
make -j$(nproc)

# 6. System-wide installation (C++ headers and libs)
echo "Installing Kompute to /usr/local..."
sudo make install
sudo ldconfig

# 7. Install the Python package (kp)
echo "Installing Kompute Python package..."
cd ..
pip3 install .

echo "------------------------------------------------"
echo "Installation Complete!"
echo "Please REBOOT your system to activate NVIDIA drivers."
echo "------------------------------------------------"
