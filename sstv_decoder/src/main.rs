use std::env;
use std::io::{self, BufReader, BufWriter, Read, Write};

use base64::engine::general_purpose::STANDARD as BASE64;
use base64::Engine as _;
use serde::Serialize;
use slowrx::{for_mode, SstvDecoder, SstvEvent, SstvMode};

const READ_BUFFER_BYTES: usize = 8192;

#[derive(Serialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum WorkerEvent {
    Ready {
        sample_rate: u32,
        version: &'static str,
    },
    VisDetected {
        mode: String,
        width: u32,
        height: u32,
        frequency_offset_hz: f64,
    },
    UnknownVis {
        code: u8,
        frequency_offset_hz: f64,
    },
    LineDecoded {
        mode: String,
        line_index: u32,
        width: u32,
        rgb_base64: String,
    },
    ImageComplete {
        mode: String,
        width: u32,
        height: u32,
        partial: bool,
        rgb_base64: String,
    },
}

fn emit(writer: &mut impl Write, event: &WorkerEvent) -> io::Result<()> {
    serde_json::to_writer(&mut *writer, event)?;
    writer.write_all(b"\n")?;
    writer.flush()
}

fn flatten_pixels(pixels: impl IntoIterator<Item = [u8; 3]>) -> Vec<u8> {
    let iterator = pixels.into_iter();
    let (minimum, _) = iterator.size_hint();
    let mut rgb = Vec::with_capacity(minimum.saturating_mul(3));
    for pixel in iterator {
        rgb.extend_from_slice(&pixel);
    }
    rgb
}

fn mode_name(mode: SstvMode) -> String {
    for_mode(mode).name.to_string()
}

fn handle_decoder_event(writer: &mut impl Write, event: SstvEvent) -> io::Result<()> {
    match event {
        SstvEvent::VisDetected {
            mode,
            hedr_shift_hz,
            ..
        } => {
            let spec = for_mode(mode);
            emit(
                writer,
                &WorkerEvent::VisDetected {
                    mode: spec.name.to_string(),
                    width: spec.line_pixels,
                    height: spec.image_lines,
                    frequency_offset_hz: hedr_shift_hz,
                },
            )?;
        }
        SstvEvent::UnknownVis {
            code,
            hedr_shift_hz,
            ..
        } => emit(
            writer,
            &WorkerEvent::UnknownVis {
                code,
                frequency_offset_hz: hedr_shift_hz,
            },
        )?,
        SstvEvent::LineDecoded {
            mode,
            line_index,
            pixels,
        } => {
            let width = for_mode(mode).line_pixels;
            emit(
                writer,
                &WorkerEvent::LineDecoded {
                    mode: mode_name(mode),
                    line_index,
                    width,
                    rgb_base64: BASE64.encode(flatten_pixels(pixels)),
                },
            )?;
        }
        SstvEvent::ImageComplete { image, partial } => {
            emit(
                writer,
                &WorkerEvent::ImageComplete {
                    mode: mode_name(image.mode),
                    width: image.width,
                    height: image.height,
                    partial,
                    rgb_base64: BASE64.encode(flatten_pixels(image.pixels)),
                },
            )?;
        }
        _ => {}
    }
    Ok(())
}

fn process_stream(
    reader: impl Read,
    writer: &mut impl Write,
    sample_rate: u32,
) -> Result<(), Box<dyn std::error::Error>> {
    let mut decoder = SstvDecoder::new(sample_rate)?;
    emit(
        writer,
        &WorkerEvent::Ready {
            sample_rate,
            version: "slowrx.rs 0.5.3",
        },
    )?;

    let mut reader = BufReader::new(reader);
    let mut buffer = [0_u8; READ_BUFFER_BYTES];
    let mut carry = None;
    loop {
        let count = reader.read(&mut buffer)?;
        if count == 0 {
            break;
        }
        let mut samples = Vec::with_capacity((count + usize::from(carry.is_some())) / 2);
        let mut offset = 0;
        if let Some(low) = carry.take() {
            samples.push(f32::from(i16::from_le_bytes([low, buffer[0]])) / 32768.0);
            offset = 1;
        }
        while offset + 1 < count {
            samples.push(
                f32::from(i16::from_le_bytes([buffer[offset], buffer[offset + 1]])) / 32768.0,
            );
            offset += 2;
        }
        if offset < count {
            carry = Some(buffer[offset]);
        }
        for event in decoder.process(&samples) {
            handle_decoder_event(writer, event)?;
        }
    }
    Ok(())
}

fn parse_sample_rate() -> Result<u32, String> {
    let value = env::args()
        .nth(1)
        .ok_or_else(|| "usage: pi-sat-sstv-decoder SAMPLE_RATE".to_string())?;
    let sample_rate = value
        .parse::<u32>()
        .map_err(|_| format!("invalid sample rate: {value}"))?;
    if !(8_000..=192_000).contains(&sample_rate) {
        return Err(format!("sample rate out of range: {sample_rate}"));
    }
    Ok(sample_rate)
}

fn main() {
    let sample_rate = match parse_sample_rate() {
        Ok(value) => value,
        Err(message) => {
            eprintln!("{message}");
            std::process::exit(2);
        }
    };
    let stdout = io::stdout();
    let mut writer = BufWriter::new(stdout.lock());
    if let Err(error) = process_stream(io::stdin().lock(), &mut writer, sample_rate) {
        eprintln!("slowrx worker failed: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use slowrx::__test_support::{mode_robot::encode_robot, vis::synth_vis};

    #[test]
    fn synthetic_pcm_stream_emits_line_and_complete_events() {
        let mode = SstvMode::Robot24;
        let spec = for_mode(mode);
        let pixels = vec![[96, 128, 128]; (spec.line_pixels * spec.image_lines) as usize];
        let mut audio = synth_vis(spec.vis_code, 0.25);
        audio.extend(encode_robot(mode, &pixels));
        // slowrx intentionally waits for 3% whole-image sync headroom before
        // its current API emits the decoded line batch.
        audio.resize(audio.len() + 11_025 * 2, 0.0);
        let pcm: Vec<u8> = audio
            .into_iter()
            .flat_map(|sample| {
                let value = (sample.clamp(-1.0, 1.0) * 32767.0).round() as i16;
                value.to_le_bytes()
            })
            .collect();

        let mut output = Vec::new();
        process_stream(&pcm[..], &mut output, 11_025).expect("synthetic PCM should decode");
        let text = String::from_utf8(output).expect("events are UTF-8 JSON lines");
        assert!(text.contains("\"type\":\"ready\""));
        assert!(text.contains("\"type\":\"vis_detected\""));
        assert!(text.contains("\"type\":\"line_decoded\""), "{text}");
        assert!(text.contains("\"type\":\"image_complete\""), "{text}");
        assert!(text.contains("\"mode\":\"Robot 24\""));
    }
}
