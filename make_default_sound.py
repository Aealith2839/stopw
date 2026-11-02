import math
import struct
import wave
from pathlib import Path

sample_rate = 44100
duration = 0.45
fade_duration = 0.05
frequencies = (880.0, 1174.66)
samples = []

for index in range(int(sample_rate * duration)):
    time = index / sample_rate
    frequency = frequencies[0] if time < duration / 2 else frequencies[1]
    value = math.sin(2 * math.pi * frequency * time)

    if time < fade_duration:
        envelope = time / fade_duration
    elif time > duration - fade_duration:
        envelope = (duration - time) / fade_duration
    else:
        envelope = 1.0

    samples.append(int(value * envelope * 0.55 * 32767))

output = Path("sounds/complete.wav")
with wave.open(str(output), "wb") as file:
    file.setnchannels(1)
    file.setsampwidth(2)
    file.setframerate(sample_rate)
    file.writeframes(b"".join(
        struct.pack("<h", sample) for sample in samples))

print(f"Created {output}")
