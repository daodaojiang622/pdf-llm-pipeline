FROM public.ecr.aws/lambda/python:3.9

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    LD_LIBRARY_PATH=/usr/local/lib:/var/lang/lib:/opt/lib \
    PKG_CONFIG_PATH=/usr/local/lib/pkgconfig \
    PATH=/usr/local/bin:$PATH

# Install system dependencies (using yum since this is Amazon Linux)
RUN yum update -y && \
    yum install -y gcc gcc-c++ make git \
    mesa-libGL glib2-devel libSM libXrender libXext \
    pkgconfig wget \
    nasm tar gzip bzip2 \
    && yum clean all

# Install newer CMake version
# Match the build architecture (x86_64 for Lambda, aarch64 on Apple silicon)
RUN CMAKE_DIR="cmake-3.20.0-linux-$(uname -m)" && \
    wget "https://github.com/Kitware/CMake/releases/download/v3.20.0/${CMAKE_DIR}.tar.gz" && \
    tar -xzf "${CMAKE_DIR}.tar.gz" && \
    cp -r "${CMAKE_DIR}"/bin/* /usr/local/bin/ && \
    cp -r "${CMAKE_DIR}"/share/* /usr/local/share/ && \
    rm -rf "${CMAKE_DIR}" "${CMAKE_DIR}.tar.gz" && \
    cmake --version

# ARM builds only: fetch the raw ARM SVE header (a /blob/ URL returns an HTML page)
RUN if [ "$(uname -m)" = "aarch64" ]; then \
        mkdir -p /usr/local/include && \
        wget -O /usr/local/include/arm_sve.h "https://raw.githubusercontent.com/gcc-mirror/gcc/master/gcc/config/aarch64/arm_sve.h"; \
    fi

# Install a specific version of FFmpeg that's compatible with decord
WORKDIR /tmp
RUN wget -O ffmpeg-4.2.2.tar.bz2 https://ffmpeg.org/releases/ffmpeg-4.2.2.tar.bz2 && \
    tar xjf ffmpeg-4.2.2.tar.bz2 && \
    cd ffmpeg-4.2.2 && \
    ./configure --prefix=/usr/local --enable-shared --disable-static --disable-doc && \
    make -j$(nproc) && \
    make install && \
    cd .. && \
    rm -rf ffmpeg-4.2.2 ffmpeg-4.2.2.tar.bz2

# Install pip and dependencies
RUN pip install --upgrade pip

# Install CPU-only torch first, matching the versions pinned in requirements.txt.
# Lambda has no GPU, and the default CUDA build would add several GB to the image.
# torchtext is imported by unimernet (used by magic-pdf); 0.18.0 matches torch 2.3.
RUN pip install torch==2.3.1 torchvision==0.18.1 torchtext==0.18.0 --index-url https://download.pytorch.org/whl/cpu

# Clone and build decord with the compatible FFmpeg
RUN git clone --recursive https://github.com/dmlc/decord && \
    cd decord && \
    git checkout v0.6.0 && \
    mkdir -p build && cd build && \
    /usr/local/bin/cmake .. -DUSE_CUDA=OFF -DCMAKE_BUILD_TYPE=Release && \
    make -j$(nproc) && \
    cd ../python && \
    pip install -e .

COPY . /var/task/
RUN echo "Listing /var/task after COPY:" && ls -R /var/task/

# Copy function code
COPY . ${LAMBDA_TASK_ROOT}/

# Define environment vars for simsimd build to disable SVE
ENV SIMSIMD_TARGET_SVE=0 \
    SIMSIMD_TARGET_SVE_F16=0 \
    SIMSIMD_TARGET_SVE_BF16=0 \
    SIMSIMD_TARGET_SVE2=0

# Try to install dependencies with modified approach
RUN pip install -r ${LAMBDA_TASK_ROOT}/requirements.txt || \
    (echo "Error installing all requirements, trying with simsimd excluded..." && \
     grep -v "simsimd" ${LAMBDA_TASK_ROOT}/requirements.txt > ${LAMBDA_TASK_ROOT}/filtered-requirements.txt && \
     pip install -r ${LAMBDA_TASK_ROOT}/filtered-requirements.txt && \
     echo "Attempting simsimd installation separately..." && \
     CFLAGS="-DSIMSIMD_TARGET_SVE=0 -DSIMSIMD_TARGET_SVE_F16=0 -DSIMSIMD_TARGET_SVE_BF16=0 -DSIMSIMD_TARGET_SVE2=0" pip install simsimd || \
     echo "WARNING: simsimd package was not installed")

# Install detectron2, which magic-pdf imports at startup. It has no PyPI release,
# so build a pinned commit against the installed torch. Torch 2.x extensions need
# a newer compiler than the default GCC 7, and the build calls `which`.
RUN yum install -y gcc10 gcc10-c++ which && yum clean all && \
    pip install wheel && \
    CC=gcc10-gcc CXX=gcc10-g++ pip install --no-build-isolation \
        "git+https://github.com/facebookresearch/detectron2.git@247691a1c292654867deb35cabb27f18d75a1363" && \
    python -c "import detectron2._C"

# Set the Lambda handler
CMD [ "lambda_handler.lambda_handler" ]