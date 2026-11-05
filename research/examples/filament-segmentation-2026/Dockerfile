FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=4 \
    OPENBLAS_NUM_THREADS=4

# The Linux PyPI torch wheel includes its CUDA runtime dependencies. The host
# supplies the NVIDIA driver/device through the executor's GPU reservation.
RUN python -m pip install --no-cache-dir \
    torch==2.7.1 numpy==2.2.6 Pillow==11.2.1 scipy==1.15.3 pycocotools==2.0.10

WORKDIR /output
CMD ["python", "/candidate/main.py", "/input/manifest.json", "/output"]
