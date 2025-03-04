import warnings
warnings.simplefilter(action='ignore', category=FutureWarning)
import itertools
import os
import time
import argparse
import json
import torch
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from torch.utils.data import DataLoader
from meldataset import MelDataset, mel_spectrogram, get_dataset_filelist
from models import Generator, MultiPeriodDiscriminator, MultiScaleDiscriminator, feature_loss, generator_loss, discriminator_loss
from utils import AttrDict, build_env, plot_spectrogram, scan_checkpoint, load_checkpoint, save_checkpoint
from stft import TorchSTFT

torch.backends.cudnn.benchmark = True


def train(a, h):

    torch.cuda.manual_seed(h.seed)
    device = torch.device("cuda")

    # Model
    generator = Generator(h).to(device)
    mpd = MultiPeriodDiscriminator().to(device)
    msd = MultiScaleDiscriminator().to(device)
    stft = TorchSTFT(filter_length=h.gen_istft_n_fft, hop_length=h.gen_istft_hop_size, win_length=h.gen_istft_n_fft).to(device)

    # Resume (1/3)
    print(generator)
    os.makedirs(a.checkpoint_path, exist_ok=True)
    print("checkpoints directory : ", a.checkpoint_path)
    if os.path.isdir(a.checkpoint_path):
        cp_g = scan_checkpoint(a.checkpoint_path, 'g_')
        cp_do = scan_checkpoint(a.checkpoint_path, 'do_')
    steps = 0
    if cp_g is None or cp_do is None:
        state_dict_do = None
        last_epoch = -1
    else:
        state_dict_g = load_checkpoint(cp_g, device)
        state_dict_do = load_checkpoint(cp_do, device)
        generator.load_state_dict(state_dict_g['generator'])
        mpd.load_state_dict(state_dict_do['mpd'])
        msd.load_state_dict(state_dict_do['msd'])
        steps = state_dict_do['steps'] + 1
        last_epoch = state_dict_do['epoch']

    # Optim
    optim_g = torch.optim.AdamW(generator.parameters(), h.learning_rate, betas=[h.adam_b1, h.adam_b2])
    optim_d = torch.optim.AdamW(itertools.chain(msd.parameters(), mpd.parameters()),
                                h.learning_rate, betas=[h.adam_b1, h.adam_b2])

    # Resume (2/3)
    if state_dict_do is not None:
        optim_g.load_state_dict(state_dict_do['optim_g'])
        optim_d.load_state_dict(state_dict_do['optim_d'])

    # Sched with Resume (3/3)
    scheduler_g = torch.optim.lr_scheduler.ExponentialLR(optim_g, gamma=h.lr_decay, last_epoch=last_epoch)
    scheduler_d = torch.optim.lr_scheduler.ExponentialLR(optim_d, gamma=h.lr_decay, last_epoch=last_epoch)

    # Data
    training_filelist, validation_filelist = get_dataset_filelist(a)
    trainset = MelDataset(training_filelist,   h.segment_size, h.n_fft, h.num_mels,
                          h.hop_size, h.win_size, h.sampling_rate, h.fmin, h.fmax, split=True,
                          shuffle=True,  fmax_loss=h.fmax_for_loss, fine_tuning=a.fine_tuning, base_mels_path=a.input_mels_dir)
    train_loader = DataLoader(trainset, num_workers=h.num_workers, shuffle=False, batch_size=h.batch_size, pin_memory=True, drop_last=True)
    validset = MelDataset(validation_filelist, h.segment_size, h.n_fft, h.num_mels,
                          h.hop_size, h.win_size, h.sampling_rate, h.fmin, h.fmax, split=False,
                          shuffle=False, fmax_loss=h.fmax_for_loss, fine_tuning=a.fine_tuning, base_mels_path=a.input_mels_dir)
    validation_loader = DataLoader(validset, num_workers=1, shuffle=False, batch_size=1, pin_memory=True, drop_last=True)

    sw = SummaryWriter(os.path.join(a.checkpoint_path, 'logs'))

    generator.train()
    mpd.train()
    msd.train()
    for epoch in range(max(0, last_epoch), a.training_epochs):
        #### Epoch ########################################################################
        start = time.time()
        print(f"Epoch: {epoch+1}")

        for _, batch in enumerate(train_loader):
            #### Step #####################################################################
            start_b = time.time()
            x, y, _, y_mel = batch
            x     =     x.to(device, non_blocking=True)
            y     =     y.to(device, non_blocking=True)
            y_mel = y_mel.to(device, non_blocking=True)
            y = y.unsqueeze(1)

            # Name Convention
            ## `y`  - wave, `y_mel` - mel of wave
            ## `_r` - GT,   `_g`    - generated
            ## `_s` - MSD,  `_f`    - MPD

            # Forward
            ## for G & D
            spec, phase = generator(x)
            y_g_hat = stft.inverse(spec, phase)
            y_g_hat_mel = mel_spectrogram(y_g_hat.squeeze(1), h.n_fft, h.num_mels, h.sampling_rate, h.hop_size, h.win_size, h.fmin, h.fmax_for_loss)

            #### Discriminators ##########################################
            optim_d.zero_grad()
            # Forard
            y_df_hat_r, y_df_hat_g, _, _ = mpd(y, y_g_hat.detach())
            y_ds_hat_r, y_ds_hat_g, _, _ = msd(y, y_g_hat.detach())
            # Loss
            loss_disc_f = discriminator_loss(y_df_hat_r, y_df_hat_g)
            loss_disc_s = discriminator_loss(y_ds_hat_r, y_ds_hat_g)
            loss_disc_all = loss_disc_f + loss_disc_s
            # Backward
            ## Generated `y_g_hat` is detached, so grad is never propagated to G
            loss_disc_all.backward()
            # Optim
            optim_d.step()
            #### /Discriminators #########################################

            #### Generators ##############################################
            optim_g.zero_grad()
            # Forward
            _, y_df_hat_g, fmap_f_r, fmap_f_g = mpd(y, y_g_hat)
            _, y_ds_hat_g, fmap_s_r, fmap_s_g = msd(y, y_g_hat)
            # Loss
            loss_fm_f = feature_loss(fmap_f_r, fmap_f_g)
            loss_fm_s = feature_loss(fmap_s_r, fmap_s_g)
            loss_gen_f = generator_loss(y_df_hat_g)
            loss_gen_s = generator_loss(y_ds_hat_g)
            loss_mel = F.l1_loss(y_mel, y_g_hat_mel) * 45
            loss_gen_all = loss_gen_f + loss_gen_s + loss_fm_f + loss_fm_s + loss_mel
            # Backward
            loss_gen_all.backward()
            # Optim
            optim_g.step()
            #### /Generators #############################################

            # Logging of training results
            ## STDOUT
            if steps % a.stdout_interval == 0:
                with torch.no_grad():
                    mel_error = F.l1_loss(y_mel, y_g_hat_mel).item()
                print('Steps : {:d}, Gen Loss Total : {:4.3f}, Mel-Spec. Error : {:4.3f}, s/b : {:4.3f}'.
                        format(steps, loss_gen_all, mel_error, time.time() - start_b))
            ## Tensorboard
            if steps % a.summary_interval == 0:
                sw.add_scalar("training/gen_loss_total", loss_gen_all, steps)
                sw.add_scalar("training/mel_spec_error", mel_error,    steps)

            # Checkpointing
            if steps % a.checkpoint_interval == 0 and steps != 0:
                checkpoint_path = "{}/g_{:08d}".format(a.checkpoint_path, steps)
                save_checkpoint(checkpoint_path, {'generator': generator.state_dict()})
                checkpoint_path = "{}/do_{:08d}".format(a.checkpoint_path, steps)
                save_checkpoint(checkpoint_path, 
                                {
                                    'mpd': mpd.state_dict(),
                                    'msd': msd.state_dict(),
                                    'optim_g': optim_g.state_dict(), 'optim_d': optim_d.state_dict(),
                                    'steps': steps, 'epoch': epoch,
                                })

            # Validation
            if steps % a.validation_interval == 0:
                generator.eval()
                torch.cuda.empty_cache()
                val_err_tot = 0
                with torch.no_grad():
                    for j, batch in enumerate(validation_loader):
                        x, y, _, y_mel = batch

                        spec, phase = generator(x.to(device))
                        y_g_hat = stft.inverse(spec, phase)

                        y_mel = torch.autograd.Variable(y_mel.to(device, non_blocking=True))
                        y_g_hat_mel = mel_spectrogram(y_g_hat.squeeze(1), h.n_fft, h.num_mels, h.sampling_rate, h.hop_size, h.win_size, h.fmin, h.fmax_for_loss)
                        val_err_tot += F.l1_loss(y_mel, y_g_hat_mel).item()

                        if j <= 4:
                            if steps == 0:
                                sw.add_audio('gt/y_{}'.format(j), y[0], steps, h.sampling_rate)
                                sw.add_figure('gt/y_spec_{}'.format(j), plot_spectrogram(x[0]), steps)

                            sw.add_audio('generated/y_hat_{}'.format(j), y_g_hat[0], steps, h.sampling_rate)
                            y_hat_spec = mel_spectrogram(y_g_hat.squeeze(1), h.n_fft, h.num_mels, h.sampling_rate, h.hop_size, h.win_size, h.fmin, h.fmax)
                            sw.add_figure('generated/y_hat_spec_{}'.format(j),
                                            plot_spectrogram(y_hat_spec.squeeze(0).cpu().numpy()), steps)

                    val_err = val_err_tot / (j+1)
                    sw.add_scalar("validation/mel_spec_error", val_err, steps)

                generator.train()

            steps += 1
            #### /Step ####################################################################

        scheduler_g.step()
        scheduler_d.step()
        
        print(f'Time taken for epoch {epoch+1} is {int(time.time() - start)} sec\n')
        #### /Epoch #######################################################################        


def main():
    print('Initializing Training Process..')

    # Arguments
    parser = argparse.ArgumentParser()
    ## Data/Model
    parser.add_argument('--input_wavs_dir', default='LJSpeech-1.1/wavs') # Directory directly under which .wav files exist
    parser.add_argument('--input_mels_dir', default='ft_dataset')
    parser.add_argument('--input_training_file',   default='LJSpeech-1.1/training.txt')   # .wav file name list for training
    parser.add_argument('--input_validation_file', default='LJSpeech-1.1/validation.txt') # .wav file name list for training
    parser.add_argument('--checkpoint_path', default='cp_hifigan')
    ## Config
    parser.add_argument('--config', default='')
    parser.add_argument('--training_epochs', default=3100, type=int)
    parser.add_argument('--fine_tuning', default=False, type=bool)
    ## Logging/Monitoring
    parser.add_argument('--stdout_interval',     default=   5, type=int)
    parser.add_argument('--checkpoint_interval', default=5000, type=int)
    parser.add_argument('--summary_interval',    default= 100, type=int)
    parser.add_argument('--validation_interval', default=1000, type=int)
    ## Parse
    a = parser.parse_args()

    with open(a.config) as f:
        data = f.read()

    json_config = json.loads(data)
    h = AttrDict(json_config)
    build_env(a.config, 'config.json', a.checkpoint_path)

    torch.manual_seed(h.seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(h.seed)

    train(a, h)


if __name__ == '__main__':
    main()
