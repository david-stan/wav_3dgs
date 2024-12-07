echo 'export PATH="/opt/conda/bin:$PATH"' >> ~/.bashrc
echo 'export PATH="/usr/local/cuda/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
conda env create --file environment.yml
source activate gaussian_splatting
pip install pywavelets pytorch-wavelets awscli
