import torch
import random
from pathlib import Path


def _load_data_shard(file: Path):
    # only reads the header, returns header data
    # header is 256 int32
    header = torch.from_file(f"{file}", False, 256, dtype=torch.int32)
    assert header[0] == 20240520, 'magic number mismatch in the data .bin file'
    assert header[1] == 1, 'unsupported version'
    num_tokens = int(header[2]) # number of tokens (claimed)
    with file.open('rb', buffering=0) as f:
        tokens = torch.empty(num_tokens, dtype=torch.uint8, pin_memory=True)
        f.seek(256 * 4)
        nbytes = f.readinto(tokens.numpy())
        assert nbytes == num_tokens, 'number of tokens read does not match header?'
    return tokens


class DistributedDataLoader:
    def __init__(self, filename_pattern: str, batch_size: int, rank: int, world_size: int):
        assert batch_size % world_size == 0
        self.world_size = world_size
        self.rank = rank
        self.files = sorted(Path.cwd().glob(filename_pattern))
        self.batch_size = batch_size
        self.local_batch_size = self.batch_size // self.world_size
        self.reset()

    def reset(self):
        self.next_shard = 0
        self.advance()

    def advance(self): # advance to next data shard
        self.pos = 0
        self.tokens = _load_data_shard(self.files[self.next_shard])
        self.next_shard = (self.next_shard + 1) % len(self.files)

    def next_batch(self):
        buf = self.tokens[self.pos + self.rank * self.local_batch_size:][:self.local_batch_size]
        # by @YouJiacheng: host side async is sufficient;
        # no performance improvement was observed when introducing a separate stream.
        sequence = buf.to(device="cuda", dtype=torch.int32, non_blocking=True) # inputs
        # advance current position and load next shard if necessary
        self.pos += self.batch_size
        if self.pos + self.batch_size >= len(self.tokens):
            self.advance()
        return sequence


class DistributedPaddedDataLoader(DistributedDataLoader):
    def __init__(self, filename_pattern, seq_len, process_rank, num_processes, cls_id, eos_id, pad_id, max_epochs=1):
        self.cls_id = cls_id
        self.eos_id = eos_id
        self.pad_id = pad_id
        self._leftover_tokens = torch.empty(0, dtype=torch.uint8)
        self.max_epochs = max_epochs
        super().__init__(filename_pattern, seq_len, process_rank, num_processes)

    def advance(self):
        self.pos = 0

        # handle epoch limit
        if self.next_shard // len(self.files) < self.max_epochs:
            raw_tokens = _load_data_shard(self.files[self.next_shard % len(self.files)])
            raw_tokens = torch.cat([self._leftover_tokens, raw_tokens], dim=0)
            self.next_shard += 1
        else:
            raw_tokens = self._leftover_tokens
        if not raw_tokens.numel():
            self._leftover_tokens = torch.empty(0, dtype=torch.uint8)
            self.tokens = torch.empty(0, dtype=torch.uint8)
            return
        
        # shuffle each epoch
        if self.next_shard % len(self.files) == 0:
            random.seed(self.next_shard)
            random.shuffle(self.files)

        processed_chunks = []
        curr_batch_len = 0
        eos_positions = (raw_tokens == self.eos_id).nonzero(as_tuple=True)[0]

        for i in range(len(eos_positions)):
            curr_eos = eos_positions[i]
            prev_eos_plus_one = 0 if i == 0 else eos_positions[i-1] + 1  # EOS_idx + 1 = CLS_idx
            sample = raw_tokens[prev_eos_plus_one:curr_eos+1]  # One sample: "CLS ... EOS"

            if not sample[0] == self.cls_id and sample[-1] == self.eos_id:
                print(f"Warning: sample[0]=={sample[0]}, sample[-1]=={sample[-1]}, sample.numel()=={sample.numel()}")
                print(f"\ti={i}, eos_positions[:i]=={eos_positions[:i]}")
            assert curr_batch_len < self.local_batch_size, str((curr_batch_len, self.local_batch_size))

            # if adding sample exceeds the batch size resulting in truncation, pad to end of batch, starting a fresh batch
            if len(sample) + curr_batch_len >= self.local_batch_size:
                num_pad = self.local_batch_size - curr_batch_len
                processed_chunks.append(torch.full((num_pad,), self.pad_id, dtype=torch.uint8))
                curr_batch_len = 0

            # if len(sample) > local batch size, chunk evenly, making multiple padded batches, starting a fresh batch
            if len(sample) > self.local_batch_size:
                for split_sample in torch.chunk(sample, len(sample) // self.local_batch_size + 1):
                    processed_chunks.append(split_sample)
                    num_pad = self.local_batch_size - len(split_sample)
                    processed_chunks.append(torch.full((num_pad,), self.pad_id, dtype=torch.uint8))
                curr_batch_len = 0
                continue

            processed_chunks.append(sample)
            curr_batch_len += len(sample)
            curr_batch_len = 0 if curr_batch_len == self.local_batch_size else curr_batch_len

        self._leftover_tokens = raw_tokens[curr_eos+1:]
        self.tokens = torch.cat(processed_chunks, dim=0)