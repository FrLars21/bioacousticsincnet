import time
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import sys
import os
from pathlib import Path
import csv
import soundfile as sf
import librosa

from sincnet_io import AudioDataset, create_dataloaders
from SincNetModel import SincNetModel, SincNetConfig

# Set up device
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
print(f"Using device: {device}")

# Set up model and configuration
cfg = SincNetConfig()
model = SincNetModel(cfg).to(device)

# Remove or conditionally apply torch.compile()
# if torch.__version__ >= "2.0" and sys.version_info < (3, 12):
#     model = torch.compile(model)
# else:
#     print("torch.compile() is not available. Using the model without compilation.")

class DataLoaderLite:
    def __init__(self, batch_size, datadir, data_list, sample_rate, cw_len):
        self.batch_size = batch_size
        self.datadir = Path(datadir)
        self.sample_rate = sample_rate
        self.chunk_length = int(cw_len * sample_rate / 1000)  # Convert ms to samples
        self.augment_factor = 0.2

        with open(data_list, 'r') as csvfile:
            self.data_list = list(csv.DictReader(csvfile))
        self.num_samples = len(self.data_list)

    def next_batch(self):

        batch_inputs = []
        batch_labels = []

        # use PyTorch to sample batch_size random rows from the data_list
        indices = torch.randperm(self.num_samples)[:self.batch_size]
        sampled_rows = [self.data_list[i] for i in indices]

        for row in sampled_rows:
            file_path = self.datadir / row['file']
            signal, fs = sf.read(str(file_path))
            
            # Convert to tensor and move to the appropriate device
            signal = torch.tensor(signal, dtype=torch.float32)

            # Ensure the signal is single-channel
            if signal.dim() == 2:
                print(f"WARNING: converting stereo to mono: {row['file']}")
                signal = signal.mean(dim=1)  # Convert stereo to mono by averaging channels
            elif signal.dim() > 2:
                raise ValueError(f"Unexpected number of dimensions in audio file: {row['file']}")

            t_min, t_max = int(float(row['start']) * fs), int(float(row['start']) * fs + float(row['length']) * fs)
        
            if t_max - t_min > self.chunk_length:
                start = torch.randint(t_min, t_max - self.chunk_length + 1, (1,)).item()
                signal = signal[start:start + self.chunk_length]
            else:
                start = torch.randint(max(0, t_max - self.chunk_length), min(t_min, signal.shape[0] - self.chunk_length) + 1, (1,)).item()
                signal = signal[start:start + self.chunk_length]

            # Pad if necessary
            if len(signal) < self.chunk_length:
                signal = F.pad(signal, (0, self.chunk_length - len(signal)))

            # Apply random amplitude scaling
            amp_scale = torch.FloatTensor(1).uniform_(1 - self.augment_factor, 1 + self.augment_factor)
            signal = signal * amp_scale

            # Normalize signal
            signal = signal / torch.abs(signal.max())

            batch_inputs.append(signal)
            batch_labels.append(int(row['label']))

        return torch.stack(batch_inputs).unsqueeze(1), torch.tensor(batch_labels, dtype=torch.long)

# Data loading
datadir = Path("data")
train_loader = DataLoaderLite(batch_size=cfg.batch_size, datadir=datadir, data_list="mod_all_classes_train_files.csv", sample_rate=cfg.sample_rate, cw_len=cfg.cw_len)
with open("mod_all_classes_test_files.csv", 'r') as csvfile:
    test_files_list = list(csv.DictReader(csvfile))
num_test_samples = len(test_files_list)

# Set up data loaders
# train_loader, val_loader = create_dataloaders(
#     root_dir="nips4bplus",
#     batch_size=256,
#     sample_rate=cfg.sample_rate,
#     cw_len=cfg.cw_len,
#     augment_factor=0
# )

# Print dataset sizes and number of batches
# print(f"Number of training samples: {len(train_loader.dataset)}")
# print(f"Number of validation samples: {len(val_loader.dataset)}")
# print(f"Number of batches per epoch: {len(train_loader)}")

# Print the number of parameters in the model
trainable_params = sum(p.numel() for p in model.parameters() if p.requires_grad)
print(f"Total number of trainable parameters in the model: {trainable_params:,}")

log_file = os.path.join(os.path.dirname(__file__), "trainlog.txt")
with open(log_file, "w") as f: # open for writing to clear the file
    pass

criterion = nn.CrossEntropyLoss()
optimizer = optim.AdamW(model.parameters())

num_epochs = 200
batches_per_epoch = 2

for epoch in range(num_epochs):
    model.train()
    train_loss = 0
    
    epoch_start_time = time.time()

    # Training loop
    for batch_idx in range(batches_per_epoch):
        x, y = train_loader.next_batch()
        x, y = x.to(device), y.to(device)
        optimizer.zero_grad()
        outputs = model(x)
        loss = criterion(outputs, y)
        loss.backward()
        optimizer.step()
        train_loss += loss.item()

        # log train loss per step
        with open(log_file, "a") as f:
            f.write(f"{epoch * batches_per_epoch + batch_idx} train {loss.item():.4f}\n")
    
    if torch.cuda.is_available(): torch.cuda.synchronize()
    train_end_time = time.time()
    
    avg_train_loss = train_loss / batches_per_epoch
    
    # Set model to evaluation mode
    model.eval()

    eval_start_time = time.time()

    with torch.no_grad():
        total_loss = 0
        total_frame_error = 0
        total_sentence_error = 0

        chunk_length = int(cfg.cw_len * cfg.sample_rate / 1000)
        chunk_shift = int(cfg.cw_shift * cfg.sample_rate / 1000)

        for sample_idx in range(num_test_samples):
            # Load and preprocess audio file
            audio_path = datadir / test_files_list[sample_idx]['file']
            signal, sample_rate = sf.read(audio_path)

            if sample_rate != cfg.sample_rate:
                signal = librosa.resample(signal, sample_rate, cfg.sample_rate)
            
            if signal.ndim > 1:
                signal = signal.mean(axis=1)

            signal = torch.from_numpy(signal).float().to(device)
            true_label = int(test_files_list[sample_idx]['label'])

            # Normalize signal
            signal = signal / torch.abs(signal).max()
            
            # Pad if necessary
            if len(signal) < chunk_length:
                signal = F.pad(signal, (0, chunk_length - len(signal)))

            # Calculate number of frames
            num_frames = max(1, (len(signal) - chunk_length) // chunk_shift + 1)
            
            # Prepare all frames
            all_frames = torch.stack([
                signal[i*chunk_shift:i*chunk_shift+chunk_length] 
                for i in range(num_frames)
            ]).unsqueeze(1)

            # Process frames in batches
            frame_logits = []
            for batch_start in range(0, num_frames, cfg.batch_size):
                batch_end = min(batch_start + cfg.batch_size, num_frames)
                batch_frames = all_frames[batch_start:batch_end]
                batch_output = model(batch_frames)
                frame_logits.append(batch_output)

            frame_logits = torch.cat(frame_logits, dim=0)

            # Calculate frame-level predictions and error
            frame_predictions = torch.argmax(frame_logits, dim=1)
            frame_labels = torch.full((num_frames,), true_label, dtype=torch.long, device=device)
            frame_error = (frame_predictions != frame_labels).float().mean()

            # Calculate loss
            loss = criterion(frame_logits, frame_labels)

            # Calculate sentence-level prediction and error
            sentence_logits = torch.sum(frame_logits, dim=0)
            sentence_prediction = sentence_logits.argmax()
            sentence_error = (sentence_prediction != frame_labels[0]).float()

            # Accumulate metrics
            total_loss += loss.item()
            total_frame_error += frame_error.item()
            total_sentence_error += sentence_error.item()

        # Calculate average metrics
        avg_loss = total_loss / num_test_samples
        avg_frame_error = total_frame_error / num_test_samples
        avg_sentence_error = total_sentence_error / num_test_samples

        if torch.cuda.is_available():
            torch.cuda.synchronize()
        eval_end_time = time.time()

        if torch.cuda.is_available(): torch.cuda.synchronize()
        eval_duration = eval_end_time - eval_start_time
        train_duration = train_end_time - epoch_start_time
        epoch_duration = eval_end_time - epoch_start_time

        print(f"Epoch {epoch+1}/{num_epochs} | "
              f"Train Loss: {avg_train_loss:.4f} | "
              f"Val Loss: {avg_loss:.4f} | "
              f"Frame Error: {avg_frame_error:.4f} | "
              f"Sentence Error: {avg_sentence_error:.4f} | "
              f"Train Time: {train_duration:.2f} seconds | "
              f"Eval Time: {eval_duration:.2f} seconds | "
              f"Total Time: {epoch_duration:.2f} seconds")
            
        # log epoch metrics
        with open(log_file, "a") as f:
            f.write(f"{(epoch + 1) * batches_per_epoch} val {avg_loss:.4f}\n")
