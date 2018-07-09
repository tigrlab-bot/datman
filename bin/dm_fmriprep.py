#!/usr/bin/env python 

'''
Runs fmriprep minimal processing pipeline on datman studies or individual sessions 

Usage: 
    dm_fmriprep [options] <study> 
    dm_fmriprep [options] <study> [<subjects>...] 

Arguments:
    <study>                 datman study nickname to be processed by fmriprep 
    <subjects>              List of space-separated datman-style subject IDs

Options: 
    -i, --singularity-image IMAGE     Specify a custom fmriprep singularity image to use [default='/archive/code/containers/FMRIPREP/poldrack*fmriprep*.img']
    -q, --quiet                 Only show WARNING/ERROR messages
    -v, --verbose               Display lots of logging information
    -d, --debug                 Display all logging information 
    -o, --out-dir               Location of where to output fmriprep outputs [default = /config_path/<study>/pipelines/fmriprep]
    -r, --rewrite               Overwrite if fmriprep pipeline outputs already exist in output directory
    -f, --fs-license-dir FSLISDIR          Freesurfer license path [default = /opt/quaratine/freesurfer/6.0.0/build/license.txt]
    -t, --threads NUM_THREADS              Number of threads to utilize [default : greedy, HIGHLY RECOMMEND LIMITING ON COMPUTE CLUSTERS!]
    --ignore-recon              Use this option to perform reconstruction even if already available in pipelines directory
    -d, --tmp-dir TMPDIR         Specify custom temporary directory (when using remote servers with restrictions on /tmp/ writing) 
    
Requirements: 
    FSL (fslroi) - for nii_to_bids.py

Note:
    FMRIPREP freesurfer module combines longitudinal data in order to enhance surface reconstruction, however sometimes we want to maintain both reconstructions 
    for temporally varying measures that are extracted from pial surfaces. 

    Thus the behaviour of the script is as follows: 
        a) If particular session is coded XX_XX_XXXX_0N where N > 1. Then the original reconstructions will be left behind and a new one will be formed 
        b) For the first run, the original freesurfer implementation will always be symbolically linked to fmriprep's reconstruction (unless a new one becomes available)  

        VERSION: TESTING
'''

import os 
import sys
import datman.config
from shutil import copytree, rmtree
import logging
import tempfile
import subprocess as proc
from docopt import docopt

logging.basicConfig(level = logging.WARN, 
        format='[%(name)s] %(levelname)s : %(message)s')
logger = logging.getLogger(os.path.basename(__file__))


#Defaults (will only work correctly in tigrlab environment -- fix) 
DEFAULT_FS_LICENSE = '/opt/quarantine/freesurfer/6.0.0/build/license.txt'
DEFAULT_SIMG = '/archive/code/containers/FMRIPREP/poldracklab_fmriprep_1.1.1-2018-06-07-2f08547a0732.img'

def get_bids_name(subject): 
    '''
    Helper function to convert datman to BIDS name
    Arguments: 
        subject                     Datman style subject ID
    '''

    return 'sub-' + subject.split('_')[1] + subject.split('_')[-2]

def configure_logger(quiet,verbose,debug): 
    '''
    Configure logger settings for script session 
    TODO: Configure log to server
    '''

    if quiet: 
        logger.setLevel(logging.ERROR)
    elif verbose: 
        logger.setLevel(logging.INFO) 
    elif debug: 
        logger.setLevel(logging.DEBUG) 
    return

def get_datman_config(study):
    '''
    Wrapper for error handling datman config instantiation 
    '''

    try: 
        config = datman.config.config(study=study)
    except KeyError: 
        logger.error('{} not a valid study ID!'.format(study))
        sys.exit(1) 

    return config

def run_bids_conversion(study,subject,config): 
    '''
    Wrapper function for running /datman/bin/nii_to_bids.py. 
    Assume it does all the validation checking so we don't have to :) 
    TODO: Add a check so we don't re-run nii-to-bids!
    '''

    nii2bds_cmd = 'nii_to_bids.py {study} {subject}'.format(study=study,subject = ' '.join(subject))

    p = proc.Popen(nii2bds_cmd, stdout=proc.PIPE, stdin=proc.PIPE, shell=True)  
    std, err = p.communicate() 

    if p.returncode: 
        logger.error('datman to BIDS conversion failed! STDERR: {}'.format(err)) 
        sys.exit(1) 

    try:
        os.listdir(os.path.join(config.get_path('data'),'bids'))
    except OSError:
        logger.error('BIDS directory failed to initialize! Please run nii_to_bids.py manually to debug!')
        logger.error('Failed command: {}'.format(nii2bds_cmd))
    return

def initialize_environment(config,subject,out_dir): 

    '''
    Initializes environment for fmriprep mounting
    Arguments: 
        config              Datman configuration object (datman.config.config)
        subject             Subject to create environment for
        out_dir             Base directory for fmriprep outputs
    '''

    try: 
        os.makedirs(os.path.join(out_dir,subject)) 
    except OSError: 
        logger.info('Path already exists, fmriprep output directories will be created within: {}'.format(out_dir))  

    bids_dir = os.path.join(config.get_path('data'),'bids') 

    return {'out' : os.path.join(out_dir,subject), 'bids' : bids_dir}
    
def fetch_fs_recon(config,subject,sub_out_dir): 
    '''
    Copies over freesurfer reconstruction to fmriprep pipeline output for auto-detection

    Arguments: 
        config                      datman.config.config object with study initialized
        subject                     datman style subject ID
        sub_out_dir                 fmriprep output directory for subject

    Output: 
        Return status
    '''
    
    #Check whether freesurfer directory exists for subject
    fs_recon_dir = os.path.join(config.get_study_base(),'pipelines','freesurfer',subject) 
    fmriprep_fs = os.path.join(sub_out_dir,'freesurfer',get_bids_name(subject)) 

    if os.path.isdir(fs_recon_dir): 
        logger.info('Located FreeSurfer reconstruction files for {}, copying (rsync) to {}'.format(subject,fmriprep_fs))

        #Create a freesurfer directory in the output directory
        try: 
            os.makedirs(fmriprep_fs) 
        except OSError: 
            logger.warning('Failed to create directory {} already exists!'.format(fmriprep_fs)) 

        #rsync source fs to fmriprep output, using os.path.join(x,'') to enforce trailing slash for rsync
        cmd = 'rsync -a {} {}'.format(os.path.join(fs_recon_dir,''),fmriprep_fs)
        p = proc.Popen(cmd, stdout=proc.PIPE, stdin=proc.PIPE, shell=True)  
        std,err = p.communicate() 

        #Error outcome
        if p.returncode: 
            logger.error('Freesurfer copying failed with error: {}'.format(err)) 
            logger.warning('fmriprep will run recon-all!')

            #Clean failed directories 
            logger.info('Cleaning created directories...')
            try: 
                os.rmtree(fmriprep_fs)
            except OSError: 
                logger.error('Failed to remove {}, please delete manually and re-run {} with --ignore-recon flag!'.format(fmriprep_fs,subject))
                logger.error('Exiting.....')
                sys.exit(1) 

            return False
        
        logger.info('Successfully copied freesurfer reconstruction to {}'.format(fmriprep_fs))
        return True
    else: 
        #No freesurfer directory found, continue on but return False status indicator

        logger.info('No freesurfer directory found in {}'.format(fs_recon_dir))
        return False 

def filter_processed(subjects, out_dir): 

    '''
    Filter out subjects that have already been previously run through fmriprep

    Arguments: 
        subjects                List of candidate subjects to be processed through pipeline
        out_dir                 Base directory for where fmriprep outputs will be placed

    Outputs: 
        List of subjects meeting criteria: 
            1) Not already processed via fmriprep
            2) Not a phantom
    '''

    criteria = lambda x: not os.path.isdir(os.path.join(out_dir,x,'fmriprep')) 
    return [s for s in subjects if criteria(s)]  
    
def gen_pbs_directives(num_threads, subject):
    '''
    Writes PBS directives into job_file
    '''

    pbs_directives = '''
    
    # PBS -l ppn={threads},walltime=24:00:00
    # PBS -V
    # PBS -N fmriprep_{name}

    cd $PBS_O_WORKDIR
    '''.format(threads=num_threads, name=subject)

    return [pbs_directives]

    
def gen_jobcmd(simg,env,subject,fs_license,num_threads,tmp_dir): 

    '''
    Generates list of job submission commands to be written into a job file
    
    Arguments: 
        simg                fmriprep singularity image
        env                 A dictionary containing fmriprep mounting directories: {base: <base directory>, work: <base/{}_work>, home: <base/{}_home>, out: <output_dir>,license: <base/{}_li}
        subject             Datman-style subject ID
        fs_license          Directory to freesurfer license.txt 
        num_threads         Number of threads

    Output: 
        [list of commands to be written into job file]
    '''
    
    #Set up environment: 
    if num_threads:
        thread_env = 'export OMP_NUM_THREADS={}'.format(num_threads)
    else: thread_env = ''

    #Cleanup function 
    trap_func = '''

    function cleanup(){
        rm -rf $HOME
    }

    '''

    #Temp initialization
    init_cmd = '''

    HOME=$(mktemp -d {home})
    WORK=$(mktemp -d $HOME/work.XXXXX)
    LICENSE=$(mktemp -d $HOME/li.XXXXX)
    BIDS={bids}
    SIMG={simg}
    SUB={sub}
    OUT={out}

    '''.format(home=os.path.join(tmp_dir,'home.XXXXX'),bids=env['bids'],simg=simg,sub=get_bids_name(subject),out=env['out'])

    #Fetch freesurfer license 
    fs_cmd =  '''
    cp {} $LICENSE/license.txt
    '''.format(fs_license if fs_license else DEFAULT_FS_LICENSE)

    
    fmri_cmd = '''

    trap cleanup EXIT 
    singularity run -H $HOME -B $BIDS:/bids -B $WORK:/work -B $OUT:/out -B $LICENSE:/li \\
    $SIMG -vvv \\
    /bids /out \\
    participant --participant-label $SUB --use-syn-sdc \\
    --fs-license-file /li/license.txt --nthreads {} 

    '''.format(num_threads)

    #Run post-cleanup if successful
    cleanup = '\n cleanup \n'

    return [thread_env,trap_func,init_cmd,fs_cmd,fmri_cmd,cleanup] 

def get_symlink_cmd(jobfile,config,subject,sub_out_dir): 
    '''
    Returns list of commands that remove original freesurfer directory and link to fmriprep freesurfer directory

    Arguments: 
        jobfile                 Path to jobfile to be modified 
        config                  datman.config.config object with study initialized
        subject                 Datman-style subject ID
        sub_out_dir             fmriprep subject output path

    Outputs: 
        [remove_cmd,symlink_cmd]    Removal of old freesurfer directory and symlinking to fmriprep version of freesurfer reconstruction
    '''

    #Path to fmriprep output and freesurfer recon directories
    fmriprep_fs_path = os.path.join(sub_out_dir,'freesurfer')
    fs_recon_dir = os.path.join(config.get_study_base(),'pipelines','freesurfer',subject) 

    #Remove entire subject directory, then symlink in the fmriprep version
    remove_cmd = '\nrm -rf {} \n'.format(fs_recon_dir) 
    symlink_cmd = 'ln -s {} {} \n'.format(fmriprep_fs_path,fs_recon_dir)
    
    return [remove_cmd, symlink_cmd]


def write_executable(f,cmds): 
    '''
    Helper script to write an executable file

    Arguments: 
        f                       Full file path
        cmds                    List of commands to write, will separate with \n
    '''
    
    header = '#!/bin/bash \n'

    with open(f,'w') as cmdfile: 
        cmdfile.write(header) 
        cmdfile.writelines(cmds)

    p = proc.Popen('chmod +x {}'.format(f), stdin=proc.PIPE, stdout=proc.PIPE, shell=True) 
    std, err = p.communicate() 
    if p.returncode: 
        logger.error('Failed to change permissions on {}'.format(f)) 
        logger.error('ERR CODE: {}'.format(err)) 
        sys.exit(1) 

    logger.info('Successfully wrote commands to {}'.format(f))

def submit_jobfile(job_file, augment_cmd=''): 

    '''
    Submit fmriprep jobfile

    Arguments: 
        job_file                    Path to fmriprep job script to be submitted
        augment_cmd                 Optional command that appends additional options to qsub
    '''

    #Formulate command
    cmd = 'qsub {job}'.format(job=job_file) + augment_cmd

    #Submit jobfile and delete after successful submission
    logger.info('Submitting job with command: {}'.format(cmd)) 
    p = proc.Popen(cmd, stdin=proc.PIPE, stdout=proc.PIPE, shell=True) 
    std,err = p.communicate() 
    
    if p.returncode: 
        logger.error('Failed to submit job, STDERR: {}'.format(err)) 
        sys.exit(1) 

    logger.info('Removing jobfile...')
    os.remove(job_file)
    
def main(): 
    
    arguments = docopt(__doc__) 

    study                       = arguments['<study>']
    subjects                     = arguments['<subjects>']

    singularity_img             = arguments['--singularity-image']

    out_dir                     = arguments['--out-dir']
    tmp_dir                     = arguments['--tmp-dir']
    fs_license                  = arguments['--fs-license-dir']

    debug                       = arguments['--debug'] 
    quiet                       = arguments['--quiet'] 
    verbose                     = arguments['--verbose'] 
    rewrite                     = arguments['--rewrite']
    ignore_recon                = arguments['--ignore-recon']
    num_threads                 = arguments['--threads']
    
    configure_logger(quiet,verbose,debug) 
    config = get_datman_config(study)
    system = config.site_config['SystemSettings'][config.system]['QUEUE']

    #Maintain original reconstruction (equivalent to ignore) 
    keeprecon = config.get_key('KeepRecon') 

    singularity_img = singularity_img if singularity_img else DEFAULT_SIMG
    DEFAULT_OUT = os.path.join(config.get_study_base(),'pipelines','fmriprep') 
    out_dir = out_dir if out_dir else DEFAULT_OUT
    tmp_dir = tmp_dir if tmp_dir else '/tmp/'

    run_bids_conversion(study, subjects, config) 
    bids_dir = os.path.join(config.get_path('data'),'bids') 

    if not subjects: 
        subjects = [s for s in os.listdir(config.get_path('nii')) if 'PHA' not in s] 

    if not rewrite: 
        subjects = filter_processed(subjects,out_dir) 

    for subject in subjects: 

        #Initialize subject directories and generate the fmriprep jobscript
        env = initialize_environment(config, subject, out_dir)

        #Generate a job file in temporary directory
        _,job_file = tempfile.mkstemp(suffix='fmriprep_job',dir=tmp_dir) 

        #Command formulation block
        logger.info('Generating commands...')
        pbs_directives = ['']
        if system == 'pbs': 
            pbs_directives = gen_pbs_directives(num_threads, subject) 
            augment_cmd = ''
        elif system == 'sge': 
            augment_cmd = ' -l ppn={}'.format(num_threads) if num_threads else ''
            augment_cmd += ' -N fmriprep_{}'.format(subject) 

        fmriprep_cmd = gen_jobcmd(singularity_img,env,subject,fs_license,num_threads,tmp_dir)
        symlink_cmd = [''] 
        if not ignore_recon or not keeprecon:

            fetch_flag = fetch_fs_recon(config,subject,env['out']) 
            
            if fetch_flag: 
                symlink_cmd = get_symlink_cmd(job_file,config,subject,env['out'])       

        #Write into jobfile
        write_executable(job_file, pbs_directives + fmriprep_cmd + symlink_cmd)

        import pdb
        pdb.set_trace() 

        submit_jobfile(job_file, augment_cmd) 

if __name__ == '__main__': 
    main() 
