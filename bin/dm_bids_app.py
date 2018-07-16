#!/usr/bin/env python

'''
Run BIDS-apps on DATMAN environment using JSON dictionaries to specify arguments

Usage: 
    dm_bids_app.py [options] [--exclude=TAG]... <study> <out> <json>  
    dm_bids_app.py [options] [--exclude=TAG]... <study> [<subjects> ...] <out> <json>   

Arguments: 
    <study>                         Datman study nickname
    <subjects>                      List of space separated datman-style subject IDs
    <out>                           Base directory for BIDS output
    <json>                          JSON key-value dictionary for BIDS-app argument information

Options: 
    -q, --quiet                     Only display ERROR Messages 
    -v, --verbose                   Display INFO/WARNING/ERROR Messages 
    -d, --debug                     Display DEBUG/INFO/WARNING/ERROR Messages
    -r, --rewrite                   Overwrite if outputs already exist in BIDS output directory 
    -t, --threads THREADS           Total number of threads to use 
    -d, --tmp-dir TMPDIR            Specify temporary directory, [default = $TMPDIR, if not set, /tmp/]
    -l, --log LOGDIR                Specify bids-app log output directory. Will output to /logs/<SUBJECT>_<BIDS_APP>_log.txt, [default = None]
    -e, --exclude                   TAG to remove from BIDS-app prior to running.  
    -j, --json JSON                 JSON file specifying list of arguments to pass to BIDS-app

Notes on arguments: 
    --exclude finds files in the temporary BIDS directory created using a *<TAG>* regex. 
    --json arguments must match usage of BIDS-app being used, for arguments with no key-value structure simply using ''--key': True will suffice 

    Additionally, the following arguments will NOT be parsed correctly: 
        --participant_label --> wrapper script handles this for you
        -w WORKDIR          --> tmp-dir/work becomes the workdir

Requirements: 
    FSL - nii_to_bids.py requires it to run 

Notes on BIDS-apps: 

    FMRIPREP
        FMRIPREP freesurfer module combines longitudinal data in order to enhance surface reconstruction. However sometimes we want to maintain both reconstructions for temporally varying measures extracted from pial surfaces. 
        Refer to datman.config.config, study config key KeepRecon. Where the value is true, original reconstructions will not be deleted and linked to the fmriprep output version 

Currently supported workflows: 
    1) FMRIPREP
    2) MRIQC
'''

import os
import sys
import datman.config
import logging
import tempfile
import subprocess as proc
from docopt import docopt
import json
import pdb
from functools import partial

logging.basicConfig(level=logging.WARN,
        format='[%(name)s %(levelname)s : %(message)s]')
logger = logging.getLogger(os.path.basename(__file__)) 

def configure_logger(quiet,verbose,debug): 
    '''
    Configure logger settings for script session
    '''

    if quiet:
        logger.setLevel(logging.ERROR) 
    elif verbose: 
        logger.setLevel(logging.INFO) 
    elif debug: 
        logger.setLevel(logger.DEBUG) 

def get_datman_config(study): 
    '''
    Wrapper for error handling datman config instantiation

    Arguments: 
        study                   DATMAN style study ID
    Output: 
        config                  datman.config.config object
    '''

    try: 
        config = datman.config.config(study=study) 
    except KeyError: 
        logger.error('{} not a valid study ID!'.format(study)) 
        sys.exit(1)

    if study != config.study_name: 
        logger.error('Study incorrectly entered as subject {}, please fix arguments!'.format(study)) 
        logger.error('Exiting...') 
        sys.exit(1) 

def filter_subjects(subjects,out_dir): 

    '''
    Filter out subjects that have alrady been previously run through the BIDS-app pipeline 

    Arguments: 
        subjects                List of candidate subjects to be processed through pipeline
        out_dir                 Base directory for where BIDS-app will output
    '''

    criteria = lambda x: not os.path.isdir(os.path.join(out_dir,x)) 
    return [s for s in subjects if criteria(s)] 

def get_json_args(json): 
    '''
    Read json file and return dictionary. Will fail if required arguments not found in JSON file. 

    Arguments: 
        json                Full path to JSON file

    Output: 
        j_dict              JSON-derived dictionary 
    '''

    with open(json,'r') as jfile: 
        j_dict = json.load(jfile) 

    #Validate basic JSON structure 
    req_keys = ['app','bidsargs','img']
    last_key = None
    try: 
        for k in req_keys: 
            last_key = k
            j_dict[k]
    except KeyError: 
        logger.error('BIDS-app not specified using JSON keyword {}, please specify pipeline!'.format(k))
        logger.error('Exiting process...') 
        sys.exit(1)

    #Format argument keys 
    args = get_dict_args(j_dict['bidsargs'])

    #Combine non-bids keys with formatted bids arugment keys 
    out_dict = { k : v for k,v in j_dict if k != 'bidsargs'} 
    out_dict.update(args) 

    return out_dict  

def get_exclusion_cmd(exclude): 
    '''
    Returns a deletion command for each tag in exclude 

    Arguments: 
        exclude                 List of string tags to be excluded from subject bids folder
    '''

    exclusion_cmd = '\n'.join(['find $BIDS -name *{tag}* -delete'.format(tag=tag) for tag in exclude]) 
    return exclusion_cmd 

def get_dict_args(arg_dict): 
    '''
    Format dictionary of key:value to --key:value if parameter, and --key:'' if boolean
    '''

    #Get key:value arguments and format keys
    args = {'--{} '.format(k) : v for k,v in arg_dict.items() if str(v).lower() != 'false'}

    #Convert boolean to UNIX style argument 
    args = {k : '' for k,v in arg_dict.items() if str(v).lower() == 'true'} 

    return args

def get_init_cmd(study,subject,tmp_dir,sub_dir,simg,log_tag):
    '''
    Get initialization steps prior to running BIDS-apps

    Arguments: 
        study                       DATMAN-style study shortname
        subject                     DATMAN-style subject name
        tmp_dir                     Location BIDS-App temporary directory
        sub_dir                     Location of output directory 
        simg                        Singularity image location 
        log_cmd                     A redirect toward logging
    '''

    trap_cmd = '''

    function cleanup(){
        rm -rf $APPHOME
    }

    '''

    init_cmd = ''' 

    APPHOME=$(mktemp -d {home}) 
    BIDS=$APPHOME/bids
    WORK=$APPHOME/work
    SIMG={simg}
    SUB={sub} 
    OUT={out} 

    mkdir -p $BIDS
    mkdir -p $WORK

    echo $APPHOME {log_cmd}

    '''.format(home=os.path.join(tmp_dir,'home.XXXXX'),simg=simg,
            sub=get_bids_name(subject),out=sub_dir,log_tag=log_tag)

    n2b_cmd = '''

    nii_2_bids.py {study} {subject} --bids-dir $BIDS {log_tag} 

    '''.format(study=study,subject=subject,log_tag=log_tag)

    return [trap_cmd,init_cmd,n2b_cmd]


def fmriprep_fork(bids_args,log_tag): 
    '''
    FMRIPREP MODULE 

    Generate a list of commands used to formulate the fmriprep job BASH script

    Arguments: 
        bids_args                       Dictionary for inputting arguments into BIDS call
        log_tag                         String tag for BASH stdout/err redirection to log

    Output: 
        [list of commands]
    '''

    #Validate fmriprep json arguments 
    try: 
        bids_args['fs-license'] 
    except KeyError: 
        logger.error('Cannot find fs-license key! Required for fmriprep freesurfer module.') 
        logger.error('Exiting...') 
        sys.exit(1) 

    
    #Freesurfer LICENSE handling 
    license_cmd = '''

    LICENSE=$APPHOME/li
    mkdir -p $LICENSE 
    cp {fs_license} $LICENSE/license.txt

    '''.format(fs_license=bids_args['fs-license'])

    #Get BIDS singularity call
    bids_cmd = fmriprep_cmd(bids_args,log_tag) 
    
    return [license_cmd, bids_cmd]


def fmriprep_cmd(bids_args,log_tag): 

    '''
    Formulates fmriprep bash script content to be written into job file

    Arguments: 

        bids_args                           bidsargs in JSON file
        log_tag                             String tag for BASH stout/err redirection to log

    Output: 
        bids_cmd                            Formatted singularity bids app call
       
    '''

    #Extract arguments to be passed to BIDS-app

    bids_cmd = '''

    trap cleanup EXIT
    singularity run -H $APPHOME -B $BIDS:/bids -B $WORK:/work -B $OUT:/out -B $LICENSE:/li \\
    $SIMG \\
    /bids /out participant -w /work \\
    --participant-label $SUB \\
    --fs-license-file /li/license.txt {args} {log_tag}  

    '''.format(args = ' '.join[k + v for k,v in bidsargs.items()], log_tag=log_tag)

    return bids_cmd 

def mriqc_fork(bids_args,log_tag): 
    '''
    MRIQC MODULE

    Formulates fmriprep bash script content to be written into job file

    Arguments: 
        bids_args                           bidsargs in JSON file
        log_tag                             String tag for BASH stout/err redirection to log

    Output: 
        [list of commands to be written into job file]
     
    '''

    mrqc_cmd = '''

    trap cleanup EXIT 
    singularity run -H $APPHOME -B $BIDS:/bids -B $WORK:/work -B $OUT:/out -B $LICENSE:/li \\
    $SIMG \\
    /bids /out participant -w /work \\
    --participant-label $SUB \\
    {args} {log_tag}

    '''.format(args = ' '.join([k + v for k,v in bids_args.items()]), log_tag=log_tag)

    return [mrqc_cmd] 

def write_executable(f, cmds): 
    '''
    Helper function to write to an executable file with a list of ocmmands

    Arguments: 
        f                               Full file path
        cmds                            List of commands to write on each line 
    '''

    #BASH Interpeter + exit upon error
    header = '#!/bin/bash \n set -e \n'

    with open(f,'w') as cmdfile: 
        cmdfile.write(header) 
        cmdfile.writelines(cmds) 

    os.chmod(f,0o775) 
    logger.info('Successfully wrote commands to {}'.format(f)) 
    return

def submit_jobfile(job_file,subject,ppn):

    '''
    Submit BIDS-app jobfile to queue 

    Arguments: 
        job_file                    Path to BIDSapp job script to be submitted
        subject                     DATMAN style subject ID 
    '''

    #Formulate command 
    cmd = 'qsub -l nodes=1:ppn={ppn},walltime=24:00:00 -V -N {subject} {job}'\
            .format(ppn=ppn,subject=subject,job=job_file)

    logger.info('Submitting job with command: {}'.format(cmd)) 
    p = proc.Popen(cmd, stdin=proc.PIPE, stdout=proc.PIPE, shell=True) 
    std, err = p.communicate() 

    if p.returncode: 
        logger.error('Failed to submit job, STDERR: {}'.format(err)) 
        sys.exit(1) 

    logger.info('Removing jobfile...')
    os.remove(job_file) 

def gen_log_redirect(log_dir,subject,app_name): 
    '''
    Convenient function to generate a stdout/stderr redirection to a log file 
    '''
        
    log_tag = '_{}_log.txt'.format(app_name) 
    return ' &>> {}'.format(os.path.join(log_dir,subject,log_tag))

def main():

    #Parse arguments 
    arguments = docopt(__doc__)

    study               =   arguments['<study>']
    subjects            =   arguments['<subjects>'] 
    out                 =   arguments['<out>']
    json                =   arguments['--json']

    quiet               =   arguments['--quiet']
    verbose             =   arguments['--verbose'] 
    debug               =   arguments['--debug'] 

    rewrite             =   arguments['--rewrite']     
    tmp_dir             =   arguments['--tmpdir']
    log_dir             =   arguments['--logdir']

    threads             =   arguments['--threads'] 

    #Strategy pattern dictionary 
    strat_dict = {
            'FMRIPREP' : fmriprep_fork, 
            'MRIQC'    : mriqc_fork
            }

    #Configuration
    config = get_datman_config(study) 

    #Set temporary directory for BIDS app
    try: 
        tmp_dir = tmp_dir if tmp_dir else os.environ['TMPDIR'] 
    except KeyError: 
        logger.info('No $TMPDIR variable set in shell, using /tmp/')
        tmp_dir = '/tmp'

    #Filter subjects
    subjects = subjects if subjects else \
    [s for s in os.listdir(config.get_path('nii')) if 'PHA' not in s] 
    if rewrite: 
        subjects = filter_subjects(subjects,out) 

    #JSON parsing and argument formatting
    jargs = get_json_args(json)

    #Handle logging commands 
    log_cmd = lambda x,y: ''
    if logdir: 
        log_cmd = partial(gen_log_redirect,log_dir=log_dir)

    #Handle tag exclusions
    exclude_cmd_list = ['']
    if exlude: 
        exclude_cmd_list = get_exclusion_cmd(exclude) 

    pdb.set_trace() 

    #Process subjects 
    for subject in subjects: 
        
        #Get subject directory and log tag
        sub_dir = os.path.join(out,subject) 
        log_tag = log_cmd(subject=subject,app_name=jargs['app']) 
        try: 
            os.makedirs(sub_dir) 
        except OSError: 
            logger.warning('Subject directory already exists at {}'.format(os.path.join(out,subjects)))


        #Get commands 
        init_cmd_list = get_init_cmd(study,subject,tmp_dir,sub_dir,jargs['img'],log_tag)
        bids_cmd_list = strat_dict[jargs['app']](json)

        #Write commands to executable and submit
        master_cmd = init_cmd_list + exclude_cmd_list + bids_cmd_list +  ['\n cleanup \n']
        fd, job_file = tempfile.mkstemp(suffix='datman_BIDS_job',dir=tmp_dir) 
        os.close(fd) 
        write_executable(job_file,master_cmd) 
        submit_jobfile(job_file,subject)
        
if __name__ == '__main__':
    main()
