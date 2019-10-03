#!/usr/bin/env python
"""
Process empathic accuracy experiment data.

Usage:
    dm_proc_ea.py [options] <study>

Arguments:
    <study>             study name defined in master Configuration .yml file.

Options:
    --subject SUBJID    Run on subject.
    --debug             Show lots of output.

"""
# allows matplotlib to function sans Xwindows
import matplotlib
matplotlib.use('Agg')

import os, sys
import logging
import glob
import copy
import time
import tempfile
import shutil
import yaml
import StringIO as io

import matplotlib.pyplot as plt
import numpy as np
import scipy.interpolate as interpolate

import datman.utils as utils
import datman.config as cfg
from docopt import docopt

logging.basicConfig(level=logging.WARN, format="[%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger(os.path.basename(__file__))

NODE = os.uname()[1]

def check_complete(directory, subject):
    """Checks to see if the output files have been created.
    Returns True if the files exist
    """
    expected_files = ['{}_vid_block-times_ea.1D',
                      '{}_vid_corr_push.csv',
                      '{}_vid_button-times.csv',
                      '{}_vid_vid-onsets.csv',
                      '{}_cvid_block-times_ea.1D',
                      '{}_cvid_corr_push.csv',
                      '{}_cvid_button-times.csv',
                      '{}_cvid_vid-onsets.csv',
                      '{}_glm_cvid_1stlevel.nii.gz',
                      '{}_glm_vid_1stlevel.nii.gz']

    for filename in expected_files:
        if not os.path.isfile(os.path.join(directory, subject, filename.format(subject))):
            return False

    return True


def log_parser(log):
    """
    This takes the EA task log file generated by e-prime and converts it into a
    set of numpy-friendly arrays (with mixed numeric and text fields.)

    pic -- 'Picture' lines, which contain the participant's ratings.
    res -- 'Response' lines, which contain their responses (unclear)
    vid -- 'Video' lines, which demark the start and end of trials.
    """
    # substitute for GREP -- finds 'eventtype' field.
    # required as this file has a different number of fields per line
    logname = copy.copy(log)
    log = open(log, "r").readlines()
    pic = filter(lambda s: 'Picture' in s, log)
    vid = filter(lambda s: 'Video' in s, log)

    # write out files from stringio blobs into numpy genfromtxt
    pic = np.genfromtxt(io.StringIO(''.join(pic)), delimiter='\t',
        names=['subject', 'trial', 'eventtype', 'code', 'time', 'ttime', 'uncertainty1', 'duration', 'uncertainty2', 'reqtime', 'reqduration', 'stimtype', 'pairindex'],
        dtype=['|S64'   , int    , '|S64'     , '|S64', int   , int    , int           , int       , int           , int      , int          , '|S64'    , int])

    vid = np.genfromtxt(io.StringIO(''.join(vid)), delimiter='\t',
        names=['subject', 'trial', 'eventtype', 'code', 'time', 'ttime', 'uncertainty1'],
        dtype=['|S64'   , int    , '|S64'     , '|S64', int   , int    , int])

    # ensure our inputs contain a 'MRI_start' string.
    if pic[0][3] != 'MRI_start':
        logger.error('log {} does not contain an MRI_start entry!'.format(logname))
        raise ValueError
    else:
        # this is the start of the fMRI run, all times are relative to this.
        mri_start = pic[0][7]
        return pic, vid, mri_start


def find_blocks(vid, mri_start):
    """
    Takes the start time and a vid tuple list to find the relative
    block numbers, their start times, and their type (string).
    """
    blocks = []
    onsets = []
    for v in vid:

        # we will use this to search through the response files
        block_number = v[1]

        # this is maybe useless (e.g., 'vid_4')
        block_name = v[3]

        # all time in 10000s of a sec.
        block_start = (v[4])

        # generate compressed video list
        blocks.append((block_number, block_name, block_start))
        onsets.append(block_start / 10000.0)

    return blocks, onsets


def find_ratings(pic, blk_start, blk_end, blk_start_time, duration):
    """
    Takes the response and picture tuple lists and the beginning of the current
    and next videos. This will search through all of the responses [vid_start
    < x < vid_end] and grab their timestamps. For each, it will find the
    corresponding picture rating and save that as an integer.

    All times in 10,000s of a second.

    102,103 -- person responses
    104     -- MRI responses
    """
    duration = int(duration)
    ratings = []
    pushes = []
    if blk_end == None:
        # find the final response number, take that as the end of our block
        trial_list = np.linspace(blk_start, pic[-1][1], pic[-1][1]-blk_start+1)
    else:
        # just use the beginning of the next block as our end.
        trial_list = np.linspace(blk_start, blk_end-1, blk_end-blk_start)

    # refine trial list to include only the first, last, and button presses
    responses = np.array(filter(lambda s: s[1] in trial_list, pic))
    responses = np.array(filter(lambda s: 'rating' in s[3], responses))

    # if the participant dosen't respond at all, freak out.
    if len(responses) == 0:
        ratings = np.array([5])
        return ratings, 0, 0

    for response in responses:
        ratings.append((int(response[3][-1]), response[4]))

    t = np.linspace(blk_start_time, blk_start_time+duration-1, num=duration)
    r = np.zeros(duration)

    val = 5
    last = 0
    logger.debug('looping through ratings: {}'.format(ratings))
    for rating in ratings:
        idx = np.where(t == rating[1])[0]

        # hack to save malformed data
        if len(idx) == 0:
            idx = [last + 1]
        logger.debug('last={} idx={} t={} rating={}'.format(last, idx, t, rating))

        idx = int(idx[-1])  # take last element, convert to int
        r[last:idx] = val   # fill in all the values before the button push
        val = rating[0]     # update the value to insert
        last = idx          # keep track of the last button push
    r[last:] = val          # fill in the tail end of the vector with the last recorded value
    n_pushes = len(ratings) # number of button pushes (the number of ratings)

    return r, n_pushes, ratings


def find_column_data(blk_name, rating_file):
    """
    Returns the data from the column of specified file with the specified name.
    """
    # read in column names, convert to lowercase, compare with block name
    column_names = np.genfromtxt(rating_file, delimiter=',',
                                              dtype=str)[0].tolist()
    column_names = map(lambda x: x.lower(), column_names)
    column_number = np.where(np.array(column_names) == blk_name.lower())[0]

    # read in actor ratings from the selected column, strip nans
    column_data = np.genfromtxt(rating_file, delimiter=',',
                                              dtype=float, skip_header=2)

    # deal with a single value
    if len(np.shape(column_data)) == 1:
        column_data = column_data[column_number]
    # deal with a column of values
    elif len(np.shape(column_data)) == 2:
        column_data = column_data[:,column_number]
    # complain if the supplied rating_file is a dungparty
    else:
        logger.error('{} is not formatted properly!'.format(rating_file))
        raise ValueError
    # strip off NaN values
    column_data = column_data[np.isfinite(column_data)]

    return column_data

def match_lengths(a, b):
    """
    Matches the length of vector b to vector a using linear interpolation.
    """

    interp = interpolate.interp1d(np.linspace(0, len(b)-1, len(b)), b)
    b = interp(np.linspace(0, len(b)-1, len(a)))

    return b


def zscore(data):
    """
    z-transforms input vector. If this fails, return a vector of zeros.
    """
    datalength = len(data)
    try:
        data = (data - np.mean(data)) / np.std(data)
    except:
        data = np.zeros(datalength)

    return data


def r2z(data):
    """
    Fischer's r-to-z transform on a matrix (elementwise).
    """
    return(0.5 * np.log((1+data) / (1-data)))


def process_behav_data(log, out_path, sub, trial_type, block_id):
    """
    This parses the behavioural log files for a given trial type (either
    'vid' for the empathic-accuracy videos, or 'cvid' for the circles task.

    First, the logs are parsed into list of 'picture', 'response', and 'video'
    events, as they contain a different number of columns and carry different
    information. The 'video' list is then used to find the start of each block.

    Within each block, this script goes about parsing the ratings made by
    the particpant using 'find_ratings'. The timing is extracted from the
    'response' list, and the actual rating is extracted from the 'picture'
    list.

    This is then compared with the hard-coded 'gold-standard' rating kept in
    a column of the specified .csv file. The lengths of these vectors are
    mached using linear interpolaton, and finally correlated. This correlation
    value is used as an amplitude modulator of the stimulus box-car. Another
    set of amplitude-modulated regressor of no interest is added using the
    number of button presses per run.

    The relationship between these ratings are written out to a .pdf file for
    visual inspection, however, the onsets, durations, and correlation values
    are only returned for the specified trial type. This should allow you to
    easily write out a GLM timing file with the onsets, lengths,
    correlations, and number of button-pushes split across trial types.
    """
    logger.debug('Processing behaviour log: {} for: {}'.format(sub,log))
    assets = os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, 'assets')

    # make sure our trial type inputs are valid
    if trial_type not in ['vid', 'cvid']:
        logger.error('trial_type input {} is incorrect: invalid vid or cvid'.format(trial_type))
        raise ValueError

    try:
        pic, vid, mri_start = log_parser(log)
    except Exception, e:
        logger.error('Failed to parse log file: {}'.format(log))
        raise e

    logger.debug('Finding blocks')
    blocks, onsets = find_blocks(vid, mri_start)
    logger.debug('Found {} blocks'.format(len(blocks)))

    durations = []
    correlations = []
    onsets_used = []
    button_pushes = []
    all_ratings = []

    # format our output plot
    width, height = plt.figaspect(1.0/len(blocks))
    fig, axs = plt.subplots(1, len(blocks), figsize=(width, height*0.8))

    # Blocks seem to refer to videos within a block
    for i in np.linspace(0, len(blocks)-1, len(blocks)).astype(int).tolist():
        logger.debug('Processing block {}'.format(i))

        blk_start = blocks[i][0]
        blk_start_time = blocks[i][2]

        # block end is the beginning of the next trial
        try:
            blk_end = blocks[i+1][0]
        # unless we are on the final trial of the block, then we return None
        except:
            blk_end = None

        blk_name = blocks[i][1]

        gold_rate = find_column_data(blk_name, os.path.join(assets, 'EA-timing.csv'))
        duration = find_column_data(blk_name, os.path.join(assets, 'EA-vid-lengths.csv'))[0]

        logger.debug('Finding ratings for block {}'.format(i))
        subj_rate, n_pushes, ratings = find_ratings(pic, blk_start, blk_end, blk_start_time, duration*10000)
        logger.debug('Found {} ratings for {} events'.format(len(subj_rate), n_pushes))

        # save a copy of the raw rating vector for the subject
        np.savetxt('{}/{}_{}_DEBUG.csv'.format(out_path, sub, blk_name), subj_rate, delimiter=',')

        logger.debug('Interpolating subject ratings to match gold standard')
        if n_pushes != 0:
            subj_rate = match_lengths(gold_rate, subj_rate)
        else:
            subj_rate = np.repeat(5, len(gold_rate))

        # save a copy of the length-matched rating vector for the subject
        np.savetxt('{}/{}_{}_ratings.csv'.format(out_path, sub, blk_name), subj_rate, delimiter=',')

        # z-score both ratings, correlate, and then zscore correlation value
        logger.debug('calcuating z-scored correlations')
        gold_rate = zscore(gold_rate)
        subj_rate = zscore(subj_rate)
        corr = np.corrcoef(subj_rate, gold_rate)[1][0]
        if np.isnan(corr):
            corr = 0  # this happens when we get no responses
        corr = r2z(corr)


        logger.debug('Adding data to plot')
        axs[i].plot(gold_rate, color='black', linewidth=2)
        axs[i].plot(subj_rate, color='red', linewidth=2)
        axs[i].set_title(blk_name + ': z(r) = ' + str(corr), size=10)
        axs[i].set_xlim((0,len(subj_rate)-1))
        axs[i].set_xlabel('TR')
        axs[i].set_xticklabels([])
        axs[i].set_ylim((-3, 3))
        if i == 0:
            axs[i].set_ylabel('Rating (z)')
        if i == len(blocks) -1:
            axs[i].legend(['Actor', 'Participant'], loc='best', fontsize=10, frameon=False)

        logger.debug('Skip the "other" kind of task (if cvid, skip vid)')
        if trial_type == 'vid' and blocks[i][1][0] == 'c':
            continue
        elif trial_type == 'cvid' and blocks[i][1][0] == 'v':
            continue

        # otherwise, save the output vectors in seconds
        else:
            try:
                for r in ratings:
                    #collate the button push times and correct for mri start_time
                    # the correction should make them compatible with onsets_used
                    #        appending ['new_value', 'time ms', 'block', 'vid_id']
                    all_ratings.append((r[0],r[1] - mri_start, block_id, blocks[i][1]))
            except TypeError:
                logger.warn('No ratings found for block {}'.format(i))
            onsets_used.append((blocks[i][1], onsets[i] - mri_start/10000.0, block_id))
            durations.append(duration.tolist())

            if type(corr) == int:
                correlations.append(corr)
            else:
                correlations.append(corr.tolist())
            # button pushes per minute (duration is in seconds)
            button_pushes.append(n_pushes / (duration.tolist() / 60.0))

    plot_name = os.path.splitext(os.path.basename(log))[0]
    logger.debug('Saving figure {}.pdf'.format(plot_name))
    fig.suptitle(plot_name, size=10)
    fig.set_tight_layout(True)
    fig.savefig('{}/{}_{}.pdf'.format(out_path, sub, plot_name))

    return onsets_used, durations, correlations, button_pushes, all_ratings

def generate_analysis_script(subject, inputs, input_type, config, study):
    """
    This writes the analysis script to replicate the methods in Harvey et al
    2013 Schizophrenia Bulletin. It expects timing files to exist.

    Briefly, this method uses the correlation between the empathic ratings of
    the participant and the actor from each video to generate an amplitude-
    modulated box-car model to be fit to each time-series. This model is
    convolved with an HRF, and is run alongside a standard boxcar. This allows
    us to detect regions that modulate their 'activation strength' with
    empathic accruacy, and those that generally track the watching of
    emotionally-valenced videos (but do not parametrically modulate).
    Since each video is of a different length, each block is encoded as such
    in the stimulus-timing file (all times in seconds):
        [start_time]*[amplitude]:[block_length]
        30*5:12
    See '-stim_times_AM2' in AFNI's 3dDeconvolve 'help' for more.
    """
    study_base = config.get_study_base(study)
    subject_dir = os.path.join(study_base, config.get_path('fmri'), 'ea', subject)
    script = '{subject_dir}/{subject}_glm_1stlevel_{input_type}.sh'.format(
        subject_dir=subject_dir, subject=subject, input_type=input_type)

    # combine motion paramaters (glob because run does not expand * any longer)
    f1 = glob.glob('{}/PARAMS/motion.*.01.1D'.format(subject_dir))[0]
    f2 = glob.glob('{}/PARAMS/motion.*.02.1D'.format(subject_dir))[0]
    f3 = glob.glob('{}/PARAMS/motion.*.03.1D'.format(subject_dir))[0]
    rtn, out = utils.run('cat {} {} {} > {}/{}_motion.1D'.format(
        f1, f2, f3, subject_dir, subject), specialquote=False)

    # get input data, turn into a single string
    input_list = inputs[input_type]
    input_list.sort()

    input_data = ''
    for i in input_list:
        input_data += '{} '.format(i)

    # open up the master script, write common variables
    f = open(script, 'wb')
    f.write("""#!/bin/bash

# clean up
rm {subject_dir}/*_glm_*

# Empathic accuracy (with amplitude modulation) GLM for {sub}.
3dDeconvolve \\
    -input {input_data} \\
    -mask {subject_dir}/anat_EPI_mask_MNI-nonlin.nii.gz \\
    -ortvec {subject_dir}/{sub}_motion.1D motion_paramaters \\
    -polort 4 \\
    -num_stimts 1 \\
    -local_times \\
    -jobs 4 \\
    -x1D {subject_dir}/{sub}_glm_vid_1stlevel_design.mat \\
    -stim_times_AM2 1 {subject_dir}/{sub}_vid_block-times_ea.1D \'dmBLOCK(1)\' \\
    -stim_label 1 empathic_accuracy \\
    -fitts {subject_dir}/{sub}_glm_vid_1stlevel_explained.nii.gz \\
    -errts {subject_dir}/{sub}_glm_vid_1stlevel_residuals.nii.gz \\
    -bucket {subject_dir}/{sub}_glm_vid_1stlevel.nii.gz \\
    -cbucket {subject_dir}/{sub}_glm_vid_1stlevel_coeffs.nii.gz \\
    -fout \\
    -tout \\
    -xjpeg {subject_dir}/{sub}_glm_vid_1stlevel_matrix.jpg

# Colour disciminiation (with amplitude modulation) GLM for {sub}.
3dDeconvolve \\
    -input {input_data} \\
    -mask {subject_dir}/anat_EPI_mask_MNI-nonlin.nii.gz \\
    -ortvec {subject_dir}/{sub}_motion.1D motion_paramaters \\
    -polort 4 \\
    -num_stimts 1 \\
    -local_times \\
    -jobs 4 \\
    -x1D {subject_dir}/{sub}_glm_cvid_1stlevel_design.mat \\
    -stim_times_AM2 1 {subject_dir}/{sub}_cvid_block-times_ea.1D \'dmBLOCK(1)\' \\
    -stim_label 1 color_videos \\
    -fitts {subject_dir}/{sub}_glm_cvid_1stlevel_explained.nii.gz \\
    -errts {subject_dir}/{sub}_glm_cvid_1stlevel_residuals.nii.gz \\
    -bucket {subject_dir}/{sub}_glm_cvid_1stlevel.nii.gz \\
    -cbucket {subject_dir}/{sub}_glm_cvid_1stlevel_coeffs.nii.gz \\
    -fout \\
    -tout \\
    -xjpeg {subject_dir}/{sub}_glm_cvid_1stlevel_matrix.jpg

""".format(input_data=input_data, subject_dir=subject_dir, sub=subject))
    f.close()

    return script

def get_inputs(files, config):
    """
    finds the inputs for the ea experiment, 3 for each epitome stage.
    """
    inputs = {}
    for exported in config.study_config['fmri']['ea']['glm']:
        candidates = filter(lambda x: '{}.nii.gz'.format(exported) in x, files)
        tagged_candidates = []

        # if a string entry, convert to a list so we iterate over elements, not letters
        tags = config.study_config['fmri']['ea']['tags']
        if type(tags) == str:
            tags = [tags]

        for tag in tags:
            logger.debug('searching for inputs with tag _{}_'.format(tag))
            tagged_candidates.extend(filter(lambda x: '_{}_'.format(tag) in x, candidates))

        if len(tagged_candidates) == 3:
            inputs[exported] = tagged_candidates
        else:
            logger.error('did not find exactly 3 inputs')
            raise Exception(tagged_candidates)

    return inputs

def analyze_subject(subject, config, study):
    """
    1) finds the behavioural log files
    2) generates the stimulus timing files from these logs
    3) finds the pre-processed fmri data
    4) runs the standard GLM analysis on these data
    """
    study_base = config.get_study_base(study)
    resources_dir = os.path.join(study_base, config.get_path('resources'))
    ea_dir = os.path.join(study_base, config.get_path('fmri'), 'ea')
    output_dir = utils.define_folder(os.path.join(study_base, config.get_path('fmri'), 'ea', subject))

    # check if subject has already been processed
    if check_complete(ea_dir, subject):
        msg = '{} already analysed'.format(subject)
        logger.info(msg)
        sys.exit(0)

    # reset / remove error.log
    error_log = os.path.join(output_dir, 'error.log')
    if os.path.isfile(error_log):
        os.remove(error_log)

    # find the behavioural data, and exit if we fail to find it
    try:
        resdirs = glob.glob(os.path.join(resources_dir, subject + '_??'))
        resources = []
        for resdir in resdirs:
            resfiles = [os.path.join(dp, f) for dp, dn, fn in os.walk(resdir) for f in fn]
            resources.extend(resfiles)
        logs = filter(lambda x: '.log' in x and 'UCLAEmpAcc' in x, resources)
        logs.sort()
    except:
        logger.error('No BEHAV data for {}.'.format(subject))
        sys.exit(1)

    # if we have the wrong number of logs, don't guess which to use, just fail
    if len(logs) != 3:
        error_message = 'Did not find exactly 3 logs for {}\nfound:{}.'.format(subject, logs)
        logger.error(error_message)
        with open(error_log, 'wb') as f:
            f.write('{}\n{}'.format(error_message, NODE))
        sys.exit(1)

    # parse and write the logs seperately for each experiment condition (video or shapes/colours video)
    for test_type in ['vid','cvid']:
        # extract all of the data from the logs
        on_all, dur_all, corr_all, push_all, timings_all = [], [], [], [], []
        try:
            logger.info('Parsing {} logfiles for subject'.format(len(logs), subject))
            for log in logs:
                # extract the block id from the logfilename
                block_id = os.path.splitext(os.path.basename(log))[0][-1]
                on, dur, corr, push, timings = process_behav_data(log, output_dir, subject, test_type, block_id)
                on_all.extend(on)
                dur_all.extend(dur)
                corr_all.extend(corr)
                push_all.extend(push)
                timings_all.extend(timings)
        except Exception, e:
            msg = 'Failed to parse logs for {}, with {}.'.format(subject, str(e))
            logger.error(msg)
            sys.exit(1)

        # write data to stimulus timing file for AFNI, and a QC csv
        # on_all = sorted(on_all, key=lambda x:x[1])
        timings_all = sorted(timings_all, key=lambda x: (x[2], x[3], x[1]))    # put the responses into order
        try:
            logger.info('Writing stimulus data')
            # write each stimulus time:
            #         [start_time]*[amplitude],[buttonpushes]:[block_length]
            #         30*5,0.002:12
            # OFFSET 4 TRs == 8 Seconds!
            # on = on - 8.0
            f1 = open('{}/{}_{}_block-times_ea.1D'.format(output_dir, subject, test_type), 'wb') # stim timing file
            f2 = open('{}/{}_{}_corr_push.csv'.format(output_dir, subject, test_type), 'wb')     # r values and num pushes / minute
            f3 = open('{}/{}_{}_button-times.csv'.format(output_dir, subject, test_type), 'wb')  # button responses and timings
            f4 = open('{}/{}_{}_vid-onsets.csv'.format(output_dir, subject, test_type), 'wb')    # button responses and timings
            f2.write('correlation,n-pushes-per-minute\n')
            f3.write('Block_ID,Video,Response,Timing\n')
            f4.write('Block_ID,Video, Onset\n')

            for i in range(len(on_all)):
                f1.write('{o:.2f}*{r:.2f},{p}:{d:.2f} '.format(o=on_all[i][1]-8.0, r=corr_all[i], p=push_all[i], d=dur_all[i]))
                f2.write('{r:.2f},{p}\n'.format(r=corr_all[i], p=push_all[i]))
            for timing in timings_all:
                f3.write('{b},{v},{r},{t:.2f}\n'.format(b=timing[2], v=timing[3], r=timing[0], t=timing[1]))
            for onset in on_all:
                f4.write('{b},{r},{t:.2f}\n'.format(b=onset[2], r=onset[0], t=onset[1]))
            f1.write('\n') # add newline at the end of each run (up to 3 runs.)
        except IOError as e:
            msg = 'Failed to open block_times & corr_push for {} with excuse {}'.format(subject, e.strerror)
            logger.error(msg)
        finally:
            f1.close()
            f2.close()
            f3.close()
            f4.close()

    # run the GLM
#    files = glob.glob(os.path.join(ea_dir, subject + '/*.nii.gz'))
#    inputs = get_inputs(files, config)
#
#    for input_type in inputs.keys():
#
#        script = generate_analysis_script(subject, inputs, input_type, config, study)
#        rtn, out = utils.run('chmod 754 {}'.format(script))
#        rtn, out = utils.run(script)
#        if rtn:
#            logger.error('Script {} failed to run on subject {} with error:\n{}'.format(
#                script, subject, out))
#            sys.exit(1)

def main():
    arguments   = docopt(__doc__)

    study   = arguments['<study>']
    subject = arguments['--subject']
    debug   = arguments['--debug']

    logging.info('Starting')
    if debug:
        logger.setLevel(logging.DEBUG)

    # load config for study
    try:
        config = cfg.config(study=study)
    except ValueError:
        logger.error('study {} not defined'.format(study))
        sys.exit(1)

    study_base = config.get_study_base(study)

    if 'ea' not in config.study_config['fmri'].keys():
        logger.error('ea not defined in fmri in {}'.format(config_file))
        sys.exit(1)

    for k in ['nii', 'fmri', 'hcp']:
        if k not in config.get_key('Paths'):
            logger.error("paths:{} not defined in {}".format(k, config_file))
            sys.exit(1)

    ea_dir = os.path.join(study_base, config.get_path('fmri'), 'ea')
    nii_dir = os.path.join(study_base, config.get_path('nii'))

    if subject:
        subjects = [subject]
    else:
        subjects=glob.glob('{}/*'.format(nii_dir))



    for subject in subjects:
        if '_PHA_' in subject:
            logger.error("{} if a phantom, cannot analyze".format(subject))
            continue
        analyze_subject(subject,config,study)






#    if subject:
#        if '_PHA_' in subject:
#            logger.error("{} is a phantom, cannot analyze".format(subject))
#            sys.exit(1)
#        analyze_subject(subject, config, study)
#
#    else:
#        # batch mode
#        subjects = glob.glob('{}/*'.format(nii_dir))
#        commands = []
#
#        if debug:
#            opts = '--debug'
#        else:
#            opts = ''
#
#        for path in subjects:
#            subject = os.path.basename(path)
#            if check_complete(ea_dir, subject):
#                logger.debug('{} already analysed'.format(subject))
#            else:
#                commands.append(" ".join([__file__, study, '--subject {}'.format(subject), opts]))
#
#        if commands:
#            logger.debug("queueing up the following commands:\n"+'\n'.join(commands))
#            for i, cmd in enumerate(commands):
#                jobname = "dm_ea_{}_{}".format(i, time.strftime("%Y%m%d-%H%M%S"))
#                jobfile = '/tmp/{}'.format(jobname)
#                logfile = '/tmp/{}.log'.format(jobname)
#                errfile = '/tmp/{}.err'.format(jobname)
#
#                with open(jobfile, 'wb') as fid:
#                    fid.write('#!/bin/bash\n')
#                    fid.write(cmd)
#
#                rtn, out = utils.run('qsub -V -q main.q -o {} -e {} -N {} {}'.format(
#                    logfile, errfile, jobname, jobfile))
#                # qbacth method -- might bring it back, but not needed
#                #fd, path = tempfile.mkstemp()
#                #os.write(fd, '\n'.join(commands))
#                #os.close(fd)
#                #rtn, out, err = utils.run('qbatch -i --logdir {ld} -N {name} --walltime {wt} {cmds}'.format(ld=logdir, name=jobname, wt=walltime, cmds=path))
#                if rtn:
#                    logger.error("Job submission failed\nstdout: {}".format(out))
#                    sys.exit(1)

if __name__=='__main__':
    main()
