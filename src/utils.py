import os
if 'XDG_CACHE_HOME' not in os.environ:
	os.environ['XDG_CACHE_HOME'] = os.path.realpath(os.path.join(os.getcwd(), './models/'))

if 'TORTOISE_MODELS_DIR' not in os.environ:
	os.environ['TORTOISE_MODELS_DIR'] = os.path.realpath(os.path.join(os.getcwd(), './models/tortoise/'))

if 'TRANSFORMERS_CACHE' not in os.environ:
	os.environ['TRANSFORMERS_CACHE'] = os.path.realpath(os.path.join(os.getcwd(), './models/transformers/'))

import argparse
import time
import json
import base64
import re
import urllib.request
import signal

import tqdm
import torch
import torchaudio
import music_tag
import gradio as gr
import gradio.utils

from datetime import datetime

from tortoise.api import TextToSpeech, MODELS, get_model_path
from tortoise.utils.audio import load_audio, load_voice, load_voices, get_voice_dir
from tortoise.utils.text import split_and_recombine_text
from tortoise.utils.device import get_device_name, set_device_name

import whisper

MODELS['dvae.pth'] = "https://huggingface.co/jbetker/tortoise-tts-v2/resolve/3704aea61678e7e468a06d8eea121dba368a798e/.models/dvae.pth"

args = None
tts = None
webui = None
voicefixer = None
whisper_model = None

def get_args():
	global args
	return args

def setup_args():
	global args

	default_arguments = {
		'share': False,
		'listen': None,
		'check-for-updates': False,
		'models-from-local-only': False,
		'low-vram': False,
		'sample-batch-size': None,
		'embed-output-metadata': True,
		'latents-lean-and-mean': True,
		'voice-fixer': False, # getting tired of long initialization times in a Colab for downloading a large dataset for it
		'voice-fixer-use-cuda': True,
		'force-cpu-for-conditioning-latents': False,
		'defer-tts-load': False,
		'device-override': None,
		'whisper-model': "base",
		'concurrency-count': 2,
		'output-sample-rate': 44100,
		'output-volume': 1,
	}

	if os.path.isfile('./config/exec.json'):
		with open(f'./config/exec.json', 'r', encoding="utf-8") as f:
			overrides = json.load(f)
			for k in overrides:
				default_arguments[k] = overrides[k]

	parser = argparse.ArgumentParser()
	parser.add_argument("--share", action='store_true', default=default_arguments['share'], help="Lets Gradio return a public URL to use anywhere")
	parser.add_argument("--listen", default=default_arguments['listen'], help="Path for Gradio to listen on")
	parser.add_argument("--check-for-updates", action='store_true', default=default_arguments['check-for-updates'], help="Checks for update on startup")
	parser.add_argument("--models-from-local-only", action='store_true', default=default_arguments['models-from-local-only'], help="Only loads models from disk, does not check for updates for models")
	parser.add_argument("--low-vram", action='store_true', default=default_arguments['low-vram'], help="Disables some optimizations that increases VRAM usage")
	parser.add_argument("--no-embed-output-metadata", action='store_false', default=not default_arguments['embed-output-metadata'], help="Disables embedding output metadata into resulting WAV files for easily fetching its settings used with the web UI (data is stored in the lyrics metadata tag)")
	parser.add_argument("--latents-lean-and-mean", action='store_true', default=default_arguments['latents-lean-and-mean'], help="Exports the bare essentials for latents.")
	parser.add_argument("--voice-fixer", action='store_true', default=default_arguments['voice-fixer'], help="Uses python module 'voicefixer' to improve audio quality, if available.")
	parser.add_argument("--voice-fixer-use-cuda", action='store_true', default=default_arguments['voice-fixer-use-cuda'], help="Hints to voicefixer to use CUDA, if available.")
	parser.add_argument("--force-cpu-for-conditioning-latents", default=default_arguments['force-cpu-for-conditioning-latents'], action='store_true', help="Forces computing conditional latents to be done on the CPU (if you constantyl OOM on low chunk counts)")
	parser.add_argument("--defer-tts-load", default=default_arguments['defer-tts-load'], action='store_true', help="Defers loading TTS model")
	parser.add_argument("--device-override", default=default_arguments['device-override'], help="A device string to override pass through Torch")
	parser.add_argument("--whisper-model", default=default_arguments['whisper-model'], help="Specifies which whisper model to use for transcription.")
	parser.add_argument("--sample-batch-size", default=default_arguments['sample-batch-size'], type=int, help="Sets how many batches to use during the autoregressive samples pass")
	parser.add_argument("--concurrency-count", type=int, default=default_arguments['concurrency-count'], help="How many Gradio events to process at once")
	parser.add_argument("--output-sample-rate", type=int, default=default_arguments['output-sample-rate'], help="Sample rate to resample the output to (from 24KHz)")
	parser.add_argument("--output-volume", type=float, default=default_arguments['output-volume'], help="Adjusts volume of output")
	
	parser.add_argument("--os", default="unix", help="Specifies which OS, easily")
	args = parser.parse_args()

	args.embed_output_metadata = not args.no_embed_output_metadata

	set_device_name(args.device_override)

	args.listen_host = None
	args.listen_port = None
	args.listen_path = None
	if args.listen:
		try:
			match = re.findall(r"^(?:(.+?):(\d+))?(\/.+?)?$", args.listen)[0]

			args.listen_host = match[0] if match[0] != "" else "127.0.0.1"
			args.listen_port = match[1] if match[1] != "" else None
			args.listen_path = match[2] if match[2] != "" else "/"
		except Exception as e:
			pass

	if args.listen_port is not None:
		args.listen_port = int(args.listen_port)
	
	return args

def pad(num, zeroes):
	s = ""
	for i in range(zeroes,0,-1):
		if num < 10 ** i:
			s = f"{s}0"
	return f"{s}{num}"

def generate(
	text,
	delimiter,
	emotion,
	prompt,
	voice,
	mic_audio,
	voice_latents_chunks,
	seed,
	candidates,
	num_autoregressive_samples,
	diffusion_iterations,
	temperature,
	diffusion_sampler,
	breathing_room,
	cvvp_weight,
	top_p,
	diffusion_temperature,
	length_penalty,
	repetition_penalty,
	cond_free_k,
	experimental_checkboxes,
	progress=None
):
	global args
	global tts

	try:
		tts
	except NameError:
		raise Exception("TTS is still initializing...")

	if voice != "microphone":
		voices = [voice]
	else:
		voices = []

	if voice == "microphone":
		if mic_audio is None:
			raise Exception("Please provide audio from mic when choosing `microphone` as a voice input")
		mic = load_audio(mic_audio, tts.input_sample_rate)
		voice_samples, conditioning_latents = [mic], None
	elif voice == "random":
		voice_samples, conditioning_latents = None, tts.get_random_conditioning_latents()
	else:
		progress(0, desc="Loading voice...")
		voice_samples, conditioning_latents = load_voice(voice)

	if voice_samples is not None and len(voice_samples) > 0:
		sample_voice = torch.cat(voice_samples, dim=-1).squeeze().cpu()

		conditioning_latents = tts.get_conditioning_latents(voice_samples, return_mels=not args.latents_lean_and_mean, progress=progress, slices=voice_latents_chunks, force_cpu=args.force_cpu_for_conditioning_latents)
		if len(conditioning_latents) == 4:
			conditioning_latents = (conditioning_latents[0], conditioning_latents[1], conditioning_latents[2], None)
			
		if voice != "microphone":
			torch.save(conditioning_latents, f'{get_voice_dir()}/{voice}/cond_latents.pth')
		voice_samples = None
	else:
		if conditioning_latents is not None:
			sample_voice, _ = load_voice(voice, load_latents=False)
			sample_voice = torch.cat(sample_voice, dim=-1).squeeze().cpu()
		else:
			sample_voice = None

	if seed == 0:
		seed = None

	if conditioning_latents is not None and len(conditioning_latents) == 2 and cvvp_weight > 0:
		print("Requesting weighing against CVVP weight, but voice latents are missing some extra data. Please regenerate your voice latents.")
		cvvp_weight = 0


	settings = {
		'temperature': float(temperature),

		'top_p': float(top_p),
		'diffusion_temperature': float(diffusion_temperature),
		'length_penalty': float(length_penalty),
		'repetition_penalty': float(repetition_penalty),
		'cond_free_k': float(cond_free_k),

		'num_autoregressive_samples': num_autoregressive_samples,
		'sample_batch_size': args.sample_batch_size,
		'diffusion_iterations': diffusion_iterations,

		'voice_samples': voice_samples,
		'conditioning_latents': conditioning_latents,
		'use_deterministic_seed': seed,
		'return_deterministic_state': True,
		'k': candidates,
		'diffusion_sampler': diffusion_sampler,
		'breathing_room': breathing_room,
		'progress': progress,
		'half_p': "Half Precision" in experimental_checkboxes,
		'cond_free': "Conditioning-Free" in experimental_checkboxes,
		'cvvp_amount': cvvp_weight,
	}

	if delimiter == "\\n":
		delimiter = "\n"

	if delimiter != "" and delimiter in text:
		texts = text.split(delimiter)
	else:
		texts = split_and_recombine_text(text)
 
	full_start_time = time.time()
 
	outdir = f"./results/{voice}/"
	os.makedirs(outdir, exist_ok=True)

	audio_cache = {}

	resample = None
	# not a ternary in the event for some reason I want to rely on librosa's upsampling interpolator rather than torchaudio's, for some reason
	if tts.output_sample_rate != args.output_sample_rate:
		resampler = torchaudio.transforms.Resample(
			tts.output_sample_rate,
			args.output_sample_rate,
			lowpass_filter_width=16,
			rolloff=0.85,
			resampling_method="kaiser_window",
			beta=8.555504641634386,
		)

	volume_adjust = torchaudio.transforms.Vol(gain=args.output_volume, gain_type="amplitude") if args.output_volume != 1 else None

	idx = 0
	idx_cache = {}
	for i, file in enumerate(os.listdir(outdir)):
		filename = os.path.basename(file)
		extension = os.path.splitext(filename)[1]
		if extension != ".json" and extension != ".wav":
			continue
		match = re.findall(rf"^{voice}_(\d+)(?:.+?)?{extension}$", filename)

		key = int(match[0])
		idx_cache[key] = True

	if len(idx_cache) > 0:
		keys = sorted(list(idx_cache.keys()))
		idx = keys[-1] + 1

	# I know there's something to pad I don't care
	
	idx = pad(idx, 4)

	def get_name(line=0, candidate=0, combined=False):
		name = f"{idx}"
		if combined:
			name = f"{name}_combined"
		elif len(texts) > 1:
			name = f"{name}_{line}"
		if candidates > 1:
			name = f"{name}_{candidate}"
		return name

	for line, cut_text in enumerate(texts):
		if emotion == "Custom":
			if prompt.strip() != "":
				cut_text = f"[{prompt},] {cut_text}"
		else:
			cut_text = f"[I am really {emotion.lower()},] {cut_text}"

		progress.msg_prefix = f'[{str(line+1)}/{str(len(texts))}]'
		print(f"{progress.msg_prefix} Generating line: {cut_text}")

		start_time = time.time()
		gen, additionals = tts.tts(cut_text, **settings )
		seed = additionals[0]
		run_time = time.time()-start_time
		print(f"Generating line took {run_time} seconds")
 
		if not isinstance(gen, list):
			gen = [gen]

		for j, g in enumerate(gen):
			audio = g.squeeze(0).cpu()
			name = get_name(line=line, candidate=j)
			audio_cache[name] = {
				'audio': audio,
				'text': cut_text,
				'time': run_time
			}
			# save here in case some error happens mid-batch
			torchaudio.save(f'{outdir}/{voice}_{name}.wav', audio, tts.output_sample_rate)

	for k in audio_cache:
		audio = audio_cache[k]['audio']

		if resampler is not None:
			audio = resampler(audio)
		if volume_adjust is not None:
			audio = volume_adjust(audio)

		audio_cache[k]['audio'] = audio
		torchaudio.save(f'{outdir}/{voice}_{k}.wav', audio, args.output_sample_rate)
 
	output_voices = []
	for candidate in range(candidates):
		if len(texts) > 1:
			audio_clips = []
			for line in range(len(texts)):
				name = get_name(line=line, candidate=candidate)
				audio = audio_cache[name]['audio']
				audio_clips.append(audio)
			
			name = get_name(candidate=candidate, combined=True)
			audio = torch.cat(audio_clips, dim=-1)
			torchaudio.save(f'{outdir}/{voice}_{name}.wav', audio, args.output_sample_rate)

			audio = audio.squeeze(0).cpu()
			audio_cache[name] = {
				'audio': audio,
				'text': text,
				'time': time.time()-full_start_time,
				'output': True
			}
		else:
			name = get_name(candidate=candidate)
			audio_cache[name]['output'] = True

	info = {
		'text': text,
		'delimiter': '\\n' if delimiter == "\n" else delimiter,
		'emotion': emotion,
		'prompt': prompt,
		'voice': voice,
		'seed': seed,
		'candidates': candidates,
		'num_autoregressive_samples': num_autoregressive_samples,
		'diffusion_iterations': diffusion_iterations,
		'temperature': temperature,
		'diffusion_sampler': diffusion_sampler,
		'breathing_room': breathing_room,
		'cvvp_weight': cvvp_weight,
		'top_p': top_p,
		'diffusion_temperature': diffusion_temperature,
		'length_penalty': length_penalty,
		'repetition_penalty': repetition_penalty,
		'cond_free_k': cond_free_k,
		'experimentals': experimental_checkboxes,
		'time': time.time()-full_start_time,
	}

	# kludgy yucky codesmells
	for name in audio_cache:
		if 'output' not in audio_cache[name]:
			continue

		output_voices.append(f'{outdir}/{voice}_{name}.wav')
		with open(f'{outdir}/{voice}_{name}.json', 'w', encoding="utf-8") as f:
			f.write(json.dumps(info, indent='\t') )

	if args.voice_fixer and voicefixer is not None:
		fixed_output_voices = []
		for path in progress.tqdm(output_voices, desc="Running voicefix..."):
			fixed = path.replace(".wav", "_fixed.wav")
			voicefixer.restore(
				input=path,
				output=fixed,
				cuda=get_device_name() == "cuda" and args.voice_fixer_use_cuda,
				#mode=mode,
			)
			fixed_output_voices.append(fixed)
		output_voices = fixed_output_voices

	if voice is not None and conditioning_latents is not None:
		with open(f'{get_voice_dir()}/{voice}/cond_latents.pth', 'rb') as f:
			info['latents'] = base64.b64encode(f.read()).decode("ascii")

	if args.embed_output_metadata:
		for name in progress.tqdm(audio_cache, desc="Embedding metadata..."):
			info['text'] = audio_cache[name]['text']
			info['time'] = audio_cache[name]['time']

			metadata = music_tag.load_file(f"{outdir}/{voice}_{name}.wav")
			metadata['lyrics'] = json.dumps(info) 
			metadata.save()
 
	if sample_voice is not None:
		sample_voice = (tts.input_sample_rate, sample_voice.numpy())

	print(f"Generation took {info['time']} seconds, saved to '{output_voices[0]}'\n")

	info['seed'] = settings['use_deterministic_seed']
	if 'latents' in info:
		del info['latents']

	os.makedirs('./config/', exist_ok=True)
	with open(f'./config/generate.json', 'w', encoding="utf-8") as f:
		f.write(json.dumps(info, indent='\t') )

	stats = [
		[ seed, "{:.3f}".format(info['time']) ]
	]

	return (
		sample_voice,
		output_voices,
		stats,
	)

import subprocess

training_process = None
def run_training(config_path):
	try:
		print("Unloading TTS to save VRAM.")
		global tts
		del tts
		tts = None
	except Exception as e:
		pass

	global training_process
	torch.multiprocessing.freeze_support()

	cmd = ['call' '.\\train.bat', config_path] if os.name == "nt" else ['bash', './train.sh', config_path]

	print("Spawning process: ", " ".join(cmd))
	training_process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
	buffer=[]
	for line in iter(training_process.stdout.readline, ""):
		buffer.append(line)
		print(line)
		yield "".join(buffer)

	training_process.stdout.close()
	return_code = training_process.wait()
	training_process = None
	if return_code:
		raise subprocess.CalledProcessError(return_code, cmd)


def stop_training():
	if training_process is None:
		return "No training in progress"
	training_process.kill()
	training_process = None
	return "Training cancelled"

def setup_voicefixer(restart=False):
	global voicefixer
	if restart:
		del voicefixer
		voicefixer = None

	try:
		print("Initializating voice-fixer")
		from voicefixer import VoiceFixer
		voicefixer = VoiceFixer()
		print("initialized voice-fixer")
	except Exception as e:
		print(f"Error occurred while tring to initialize voicefixer: {e}")

def setup_tortoise(restart=False):
	global args
	global tts

	if args.voice_fixer and not restart:
		setup_voicefixer(restart=restart)

	if restart:
		del tts
		tts = None

	print("Initializating TorToiSe...")
	tts = TextToSpeech(minor_optimizations=not args.low_vram)
	get_model_path('dvae.pth')
	print("TorToiSe initialized, ready for generation.")
	return tts

def save_training_settings( batch_size=None, learning_rate=None, print_rate=None, save_rate=None, name=None, dataset_name=None, dataset_path=None, validation_name=None, validation_path=None ):
	settings = {
		"batch_size": batch_size if batch_size else 128,
		"learning_rate": learning_rate if learning_rate else 1e-5,
		"print_rate": print_rate if print_rate else 50,
		"save_rate": save_rate if save_rate else 50,
		"name": name if name else "finetune",
		"dataset_name": dataset_name if dataset_name else "finetune",
		"dataset_path": dataset_path if dataset_path else "./training/finetune/train.txt",
		"validation_name": validation_name if validation_name else "finetune",
		"validation_path": validation_path if validation_path else "./training/finetune/train.txt",
	}
	outfile = f'./training/{settings["name"]}.yaml'

	with open(f'./models/.template.yaml', 'r', encoding="utf-8") as f:
		yaml = f.read()

	for k in settings:
		yaml = yaml.replace(f"${{{k}}}", str(settings[k]))

	with open(outfile, 'w', encoding="utf-8") as f:
		f.write(yaml)

	return f"Training settings saved to: {outfile}"

def prepare_dataset( files, outdir, language=None, progress=None ):
	global whisper_model
	if whisper_model is None:
		notify_progress(f"Loading Whisper model: {args.whisper_model}", progress)
		whisper_model = whisper.load_model(args.whisper_model)

	os.makedirs(outdir, exist_ok=True)

	idx = 0
	results = {}
	transcription = []

	for file in enumerate_progress(files, desc="Iterating through voice files", progress=progress):
		print(f"Transcribing file: {file}")
		
		result = whisper_model.transcribe(file, language=language if language else "English")
		results[os.path.basename(file)] = result

		print(f"Transcribed file: {file}, {len(result['segments'])} found.")

		waveform, sampling_rate = torchaudio.load(file)
		num_channels, num_frames = waveform.shape

		for segment in result['segments']: # enumerate_progress(result['segments'], desc="Segmenting voice file", progress=progress):
			start = int(segment['start'] * sampling_rate)
			end = int(segment['end'] * sampling_rate)

			sliced_waveform = waveform[:, start:end]
			sliced_name = f"{pad(idx, 4)}.wav"

			torchaudio.save(f"{outdir}/{sliced_name}", sliced_waveform, sampling_rate)

			transcription.append(f"{sliced_name}|{segment['text'].strip()}")
			idx = idx + 1
	
	with open(f'{outdir}/whisper.json', 'w', encoding="utf-8") as f:
		f.write(json.dumps(results, indent='\t'))
	
	with open(f'{outdir}/train.txt', 'w', encoding="utf-8") as f:
		f.write("\n".join(transcription))

	return f"Processed dataset to: {outdir}"

def reset_generation_settings():
	with open(f'./config/generate.json', 'w', encoding="utf-8") as f:
		f.write(json.dumps({}, indent='\t') )
	return import_generate_settings()

def import_voices(files, saveAs=None, progress=None):
	global args

	if not isinstance(files, list):
		files = [files]

	for file in enumerate_progress(files, desc="Importing voice files", progress=progress):
		j, latents = read_generate_settings(file, read_latents=True)
		
		if j is not None and saveAs is None:
			saveAs = j['voice']
		if saveAs is None or saveAs == "":
			raise Exception("Specify a voice name")

		outdir = f'{get_voice_dir()}/{saveAs}/'
		os.makedirs(outdir, exist_ok=True)

		if latents:
			print(f"Importing latents to {latents}")
			with open(f'{outdir}/cond_latents.pth', 'wb') as f:
				f.write(latents)
			latents = f'{outdir}/cond_latents.pth'
			print(f"Imported latents to {latents}")
		else:
			filename = file.name
			if filename[-4:] != ".wav":
				raise Exception("Please convert to a WAV first")

			path = f"{outdir}/{os.path.basename(filename)}"
			print(f"Importing voice to {path}")

			waveform, sampling_rate = torchaudio.load(filename)

			if args.voice_fixer and voicefixer is not None:
				# resample to best bandwidth since voicefixer will do it anyways through librosa
				if sampling_rate != 44100:
					print(f"Resampling imported voice sample: {path}")
					resampler = torchaudio.transforms.Resample(
						sampling_rate,
						44100,
						lowpass_filter_width=16,
						rolloff=0.85,
						resampling_method="kaiser_window",
						beta=8.555504641634386,
					)
					waveform = resampler(waveform)
					sampling_rate = 44100

				torchaudio.save(path, waveform, sampling_rate)

				print(f"Running 'voicefixer' on voice sample: {path}")
				voicefixer.restore(
					input = path,
					output = path,
					cuda=get_device_name() == "cuda" and args.voice_fixer_use_cuda,
					#mode=mode,
				)
			else:
				torchaudio.save(path, waveform, sampling_rate)

			print(f"Imported voice to {path}")

def import_generate_settings(file="./config/generate.json"):
	settings, _ = read_generate_settings(file, read_latents=False)
	
	if settings is None:
		return None

	return (
		None if 'text' not in settings else settings['text'],
		None if 'delimiter' not in settings else settings['delimiter'],
		None if 'emotion' not in settings else settings['emotion'],
		None if 'prompt' not in settings else settings['prompt'],
		None if 'voice' not in settings else settings['voice'],
		None,
		None,
		None if 'seed' not in settings else settings['seed'],
		None if 'candidates' not in settings else settings['candidates'],
		None if 'num_autoregressive_samples' not in settings else settings['num_autoregressive_samples'],
		None if 'diffusion_iterations' not in settings else settings['diffusion_iterations'],
		0.8 if 'temperature' not in settings else settings['temperature'],
		"DDIM" if 'diffusion_sampler' not in settings else settings['diffusion_sampler'],
		8   if 'breathing_room' not in settings else settings['breathing_room'],
		0.0 if 'cvvp_weight' not in settings else settings['cvvp_weight'],
		0.8 if 'top_p' not in settings else settings['top_p'],
		1.0 if 'diffusion_temperature' not in settings else settings['diffusion_temperature'],
		1.0 if 'length_penalty' not in settings else settings['length_penalty'],
		2.0 if 'repetition_penalty' not in settings else settings['repetition_penalty'],
		2.0 if 'cond_free_k' not in settings else settings['cond_free_k'],
		None if 'experimentals' not in settings else settings['experimentals'],
	)

def curl(url):
	try:
		req = urllib.request.Request(url, headers={'User-Agent': 'Python'})
		conn = urllib.request.urlopen(req)
		data = conn.read()
		data = data.decode()
		data = json.loads(data)
		conn.close()
		return data
	except Exception as e:
		print(e)
		return None

def check_for_updates():
	if not os.path.isfile('./.git/FETCH_HEAD'):
		print("Cannot check for updates: not from a git repo")
		return False

	with open(f'./.git/FETCH_HEAD', 'r', encoding="utf-8") as f:
		head = f.read()
	
	match = re.findall(r"^([a-f0-9]+).+?https:\/\/(.+?)\/(.+?)\/(.+?)\n", head)
	if match is None or len(match) == 0:
		print("Cannot check for updates: cannot parse FETCH_HEAD")
		return False

	match = match[0]

	local = match[0]
	host = match[1]
	owner = match[2]
	repo = match[3]

	res = curl(f"https://{host}/api/v1/repos/{owner}/{repo}/branches/") #this only works for gitea instances

	if res is None or len(res) == 0:
		print("Cannot check for updates: cannot fetch from remote")
		return False

	remote = res[0]["commit"]["id"]

	if remote != local:
		print(f"New version found: {local[:8]} => {remote[:8]}")
		return True

	return False

def reload_tts():
	setup_tortoise(restart=True)

def cancel_generate():
	tortoise.api.STOP_SIGNAL = True

def get_voice_list(dir=get_voice_dir()):
	os.makedirs(dir, exist_ok=True)
	return sorted([d for d in os.listdir(dir) if os.path.isdir(os.path.join(dir, d)) and len(os.listdir(os.path.join(dir, d))) > 0 ]) + ["microphone", "random"]

def export_exec_settings( listen, share, check_for_updates, models_from_local_only, low_vram, embed_output_metadata, latents_lean_and_mean, voice_fixer, voice_fixer_use_cuda, force_cpu_for_conditioning_latents, defer_tts_load, device_override, whisper_model, sample_batch_size, concurrency_count, output_sample_rate, output_volume ):
	global args

	args.listen = listen
	args.share = share
	args.check_for_updates = check_for_updates
	args.models_from_local_only = models_from_local_only
	args.low_vram = low_vram
	args.force_cpu_for_conditioning_latents = force_cpu_for_conditioning_latents
	args.defer_tts_load = defer_tts_load
	args.device_override = device_override
	args.whisper_model = whisper_model
	args.sample_batch_size = sample_batch_size
	args.embed_output_metadata = embed_output_metadata
	args.latents_lean_and_mean = latents_lean_and_mean
	args.voice_fixer = voice_fixer
	args.voice_fixer_use_cuda = voice_fixer_use_cuda
	args.concurrency_count = concurrency_count
	args.output_sample_rate = output_sample_rate
	args.output_volume = output_volume

	settings = {
		'listen': None if args.listen else args.listen,
		'share': args.share,
		'low-vram':args.low_vram,
		'check-for-updates':args.check_for_updates,
		'models-from-local-only':args.models_from_local_only,
		'force-cpu-for-conditioning-latents': args.force_cpu_for_conditioning_latents,
		'defer-tts-load': args.defer_tts_load,
		'device-override': args.device_override,
		'whisper-model': args.whisper_model,
		'sample-batch-size': args.sample_batch_size,
		'embed-output-metadata': args.embed_output_metadata,
		'latents-lean-and-mean': args.latents_lean_and_mean,
		'voice-fixer': args.voice_fixer,
		'voice-fixer-use-cuda': args.voice_fixer_use_cuda,
		'concurrency-count': args.concurrency_count,
		'output-sample-rate': args.output_sample_rate,
		'output-volume': args.output_volume,
	}

	os.makedirs('./config/', exist_ok=True)
	with open(f'./config/exec.json', 'w', encoding="utf-8") as f:
		f.write(json.dumps(settings, indent='\t') )

def read_generate_settings(file, read_latents=True, read_json=True):
	j = None
	latents = None

	if file is not None:
		if hasattr(file, 'name'):
			file = file.name

		if file[-4:] == ".wav":
			metadata = music_tag.load_file(file)
			if 'lyrics' in metadata:
				j = json.loads(str(metadata['lyrics']))
		elif file[-5:] == ".json":
			with open(file, 'r') as f:
				j = json.load(f)

	if j is None:
		print("No metadata found in audio file to read")
	else:
		if 'latents' in j:
			if read_latents:
				latents = base64.b64decode(j['latents'])
			del j['latents']
		

		if "time" in j:
			j["time"] = "{:.3f}".format(j["time"])

	return (
		j,
		latents,
	)

def enumerate_progress(iterable, desc=None, progress=None, verbose=None):
	if verbose and desc is not None:
		print(desc)

	if progress is None:
		return tqdm(iterable, disable=not verbose)
	return progress.tqdm(iterable, desc=f'{progress.msg_prefix} {desc}' if hasattr(progress, 'msg_prefix') else desc, track_tqdm=True)

def notify_progress(message, progress=None, verbose=True):
	if verbose:
		print(message)

	if progress is None:
		return

	progress(0, desc=message)